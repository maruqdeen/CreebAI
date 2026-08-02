"""The decision pipeline.

`decide()` takes an inbound message and returns what should happen to it. It
knows nothing about Telegram and performs no I/O beyond the database and the
`Judge` it is handed, so the whole decision matrix is unit-testable without a
group, a bot token, or a network.

Order of the gate, and why:

    ignore  →  keyword gate  →  model  →  rules  →  threshold

The rules layer runs *after* the model rather than before it, even though a
trigger match means the message is being held regardless. That costs one call
and buys the admin a ready draft in the composer instead of an empty box.

The silent-capture path is the exception: it never calls the model, so those
rows land with no draft and the composer's Redraft button fills one on demand.
That is what keeps keyword discovery free.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.flows import (
    ESCALATE,
    THANKS,
    Flow,
    Step,
    classify_acknowledgement,
    load_flows,
    next_step_id,
    render_transcript,
)
from app.groq_client import Judge, Verdict
from app.kb import (
    MAX_TERM_LEN,
    extract_candidate_keywords,
    looks_like_question,
    match_keywords,
    sane_term,
)
from app.models import (
    ABANDONED,
    ACTIVE,
    ANSWERED,
    BY_BOT,
    FINISHED,
    REF_PREFIX,
    TELEGRAM,
    UNANSWERED,
    ChannelSettings,
    Conversation,
    KBEntry,
    Keyword,
    Query,
    QueryKeyword,
    Rule,
    utcnow,
)

log = logging.getLogger(__name__)

# Actions
IGNORE = "ignore"
REPLY = "reply"
QUEUE = "queue"


@dataclass
class Inbound:
    """One message off the wire, stripped of platform detail."""

    text: str
    channel: str = TELEGRAM
    chat_id: int | None = None
    message_id: int | None = None
    user_id: int | None = None
    user_handle: str | None = None
    from_bot: bool = False
    is_command: bool = False
    #: A screenshot or screen recording. Flows can ask for one; the answering
    #: pipeline ignores it, because nothing here reads images.
    has_photo: bool = False


@dataclass
class Decision:
    action: str
    reason: str
    reply_text: str | None = None
    matched_keywords: list[str] = field(default_factory=list)
    new_keywords: list[str] = field(default_factory=list)
    verdict: Verdict | None = None
    rule_ref: str | None = None
    #: Set once persisted.
    query_ref: str | None = None
    query_id: int | None = None

    @property
    def escalated(self) -> bool:
        return self.action == QUEUE


@dataclass
class ChannelContext:
    """Everything the pipeline needs about a channel, loaded once per message."""

    settings: ChannelSettings
    keywords: list[str]
    rules: list[Rule]
    entries: list[KBEntry]
    kb_corpus: str

    def in_scope(self, text: str) -> bool:
        """Whether this message is about the product at all.

        A support group is still a group: most of what is said in it is people
        talking to each other. Without this gate every generic target keyword —
        "time", "data", "free" — drags ordinary conversation into the bot.

        No product terms configured means no restriction, so an existing setup
        keeps working rather than going silent on upgrade.
        """
        terms = self.settings.product_term_list()
        return not terms or bool(match_keywords(text, terms))

    def prompt_kb(self) -> str:
        """The knowledge the model is given: the written base plus filed answers.

        Answers filed from the composer have to appear here or the loop the
        panel promises does not exist — the operator would file an answer and
        the bot would still not know it.
        """
        blocks = [(self.settings.kb_text or "").strip()]
        if self.entries:
            filed = "\n\n".join(
                f"{e.title.strip()}\n{e.body.strip()}" for e in self.entries
            )
            blocks.append("ANSWERS FILED BY THE OPERATOR\n\n" + filed)
        return "\n\n".join(b for b in blocks if b)

    @classmethod
    def load(cls, session: Session, channel: str) -> "ChannelContext":
        cs = session.scalar(select(ChannelSettings).where(ChannelSettings.channel == channel))
        if cs is None:
            cs = ChannelSettings(channel=channel, kb_text="")
            session.add(cs)
            session.flush()

        keywords = list(
            session.scalars(
                select(Keyword.term).where(Keyword.channel == channel).order_by(Keyword.term)
            )
        )
        rules = list(
            session.scalars(
                select(Rule)
                .where(Rule.channel == channel, Rule.active.is_(True))
                .order_by(Rule.position, Rule.ref)
            )
        )
        entries = list(
            session.scalars(
                select(KBEntry).where(KBEntry.channel == channel).order_by(KBEntry.id)
            )
        )
        corpus = " ".join(
            [cs.kb_text or ""] + [f"{e.title} {e.body}" for e in entries]
        ).lower()

        return cls(
            settings=cs,
            keywords=keywords,
            rules=rules,
            entries=entries,
            kb_corpus=corpus,
        )

    def rule_texts(self) -> list[str]:
        return [f"{r.ref}: {r.text}" for r in self.rules]

    def tripped_rule(self, text: str) -> Rule | None:
        """First active rule whose trigger terms appear in the message.

        Rules with no triggers are advisory: they shape the prompt but enforce
        nothing here.
        """
        for rule in self.rules:
            terms = rule.trigger_terms()
            if terms and match_keywords(text, terms):
                return rule
        return None

    def is_new_term(self, term: str) -> bool:
        """True when the term is in neither the target list nor the KB."""
        term = term.strip().lower()
        if not term:
            return False
        if term in {k.lower() for k in self.keywords}:
            return False
        return not match_keywords(self.kb_corpus, [term])


def next_ref(session: Session, channel: str) -> str:
    """Allocate the next TG-#### / EM-#### reference for a channel."""
    cs = session.scalar(select(ChannelSettings).where(ChannelSettings.channel == channel))
    if cs is None:
        cs = ChannelSettings(channel=channel, kb_text="")
        session.add(cs)
        session.flush()
    cs.ref_counter = (cs.ref_counter or 0) + 1
    return f"{REF_PREFIX.get(channel, 'QQ')}-{cs.ref_counter:04d}"


def decide(inbound: Inbound, session: Session, judge: Judge) -> Decision:
    """Work out what to do with a message. Does not write anything."""
    text = (inbound.text or "").strip()

    if inbound.from_bot:
        return Decision(IGNORE, "Message came from a bot.")
    if inbound.is_command or text.startswith("/"):
        return Decision(IGNORE, "Message is a command.")
    if not text:
        return Decision(IGNORE, "Message has no text.")

    ctx = ChannelContext.load(session, inbound.channel)

    # --- Is this even about the product? ----------------------------------
    # Before anything else: no mention of the product means other people
    # talking to each other. Not answered, not captured, not reported.
    if not ctx.in_scope(text):
        return Decision(IGNORE, "Not about the product — group conversation.")

    matched = match_keywords(text, ctx.keywords)

    # --- No target keyword -------------------------------------------------
    if not matched:
        if looks_like_question(text):
            # The discovery path. No model call, no reply — but the question is
            # recorded so a topic you have no keyword for still reaches you.
            candidates = extract_candidate_keywords(text)
            return Decision(
                QUEUE,
                "No target keyword matched, but the message reads as a question. "
                "Captured for keyword discovery.",
                new_keywords=[t for t in candidates if ctx.is_new_term(t)],
            )
        return Decision(IGNORE, "No target keyword and not question-shaped.")

    # --- Ask the model -----------------------------------------------------
    verdict = judge.judge(text, ctx.prompt_kb(), ctx.rule_texts())

    def escalate(reason: str, rule_ref: str | None = None) -> Decision:
        terms = list(dict.fromkeys(verdict.keywords + extract_candidate_keywords(text)))
        return Decision(
            QUEUE,
            reason,
            matched_keywords=matched,
            new_keywords=[t for t in terms if ctx.is_new_term(t)],
            verdict=verdict,
            rule_ref=rule_ref,
        )

    if not verdict.ok:
        # Fail closed: a model that could not be reached never gets to speak.
        return escalate(f"Model call failed, held for a human. ({verdict.error})")

    tripped = ctx.tripped_rule(text)
    if tripped is not None:
        return escalate(f"Held by rule {tripped.ref}: {tripped.text}", rule_ref=tripped.ref)

    threshold = ctx.settings.reply_threshold or 0.62
    if not verdict.covered_by_kb:
        return escalate("No knowledge base entry covers this question.")
    if verdict.confidence < threshold:
        return escalate(
            f"Model confidence {verdict.confidence:.2f} is below the "
            f"{threshold:.2f} reply threshold."
        )
    if not verdict.answer:
        return escalate("Model returned no answer text.")

    return Decision(
        REPLY,
        f"Answered from the knowledge base at {verdict.confidence:.2f} confidence.",
        reply_text=verdict.answer,
        matched_keywords=matched,
        new_keywords=[],
        verdict=verdict,
    )


def persist(session: Session, inbound: Inbound, decision: Decision) -> Query | None:
    """Write the decision. Ignored messages are never stored.

    Mutates `decision` with the allocated ref and id. The caller owns the
    transaction.
    """
    if decision.action == IGNORE:
        return None

    ctx_new = set(decision.new_keywords)
    verdict = decision.verdict
    answered = decision.action == REPLY
    now: datetime = utcnow()

    query = Query(
        ref=next_ref(session, inbound.channel),
        channel=inbound.channel,
        tg_chat_id=inbound.chat_id,
        tg_message_id=inbound.message_id,
        tg_user_id=inbound.user_id,
        user_handle=inbound.user_handle,
        body=inbound.text.strip(),
        received_at=now,
        state=ANSWERED if answered else UNANSWERED,
        answered_by=BY_BOT if answered else None,
        confidence=verdict.confidence if verdict else None,
        covered_by_kb=verdict.covered_by_kb if verdict else None,
        topic=(verdict.topic if verdict else None) or None,
        reason=decision.reason,
        draft=(verdict.answer if verdict else None) or None,
        answer_text=decision.reply_text if answered else None,
        answered_at=now if answered else None,
        latency_ms=verdict.latency_ms if verdict else None,
    )
    session.add(query)
    session.flush()

    # Keywords on every stored query: the unanswered ones drive discovery, the
    # answered ones drive "most asked" on the dashboard.
    #
    # Matched terms are the operator's own configured keywords and are trusted
    # as written (only length-clamped). Everything else is untrusted input and
    # goes through sane_term.
    terms: list[str] = []
    for term in decision.matched_keywords:
        term = (term or "").strip().lower()[:MAX_TERM_LEN]
        if term and term not in terms:
            terms.append(term)
    for raw in (
        (verdict.keywords if verdict else [])
        + decision.new_keywords
        + extract_candidate_keywords(inbound.text)
    ):
        term = sane_term(raw)
        if term and term not in terms:
            terms.append(term)

    for term in terms[:12]:
        session.add(
            QueryKeyword(query_id=query.id, term=term, is_new=term in ctx_new)
        )
    # Flush so callers see the rows without committing — persist() leaves the
    # transaction open and the caller decides when it closes.
    session.flush()

    decision.query_ref = query.ref
    decision.query_id = query.id
    return query


def handle(inbound: Inbound, session: Session, judge: Judge) -> Decision:
    """The single entry point for callers.

    Guided flows are consulted first. A flow that is already running owns the
    person's next message whatever it contains — an answer like "Nigeria MTN
    50mb" carries no target keyword and would otherwise be ignored.
    """
    ack = close_on_acknowledgement(inbound, session)
    if ack is not None:
        return ack

    flow_decision = advance_flow(inbound, session, judge=judge)
    if flow_decision is not None:
        return flow_decision

    decision = decide(inbound, session, judge)
    persist(session, inbound, decision)
    return decision


# ── Acknowledgements ───────────────────────────────────────────────────────


def close_on_acknowledgement(inbound: Inbound, session: Session) -> Decision | None:
    """Handle "thanks" and "ok" before anything else looks at the message.

    Runs ahead of the flow layer on purpose. A person saying "ok thank you" is
    closing the conversation, not answering the question they were last asked —
    reading it as an answer is how a satisfied user gets escalated to an admin.
    """
    if inbound.from_bot or inbound.is_command:
        return None

    verdict = classify_acknowledgement(inbound.text or "")
    if verdict is None:
        return None

    # Whatever they were in the middle of, they are done with it.
    convo = _active_conversation(session, inbound)
    if convo is not None:
        convo.append("user", inbound.text or "")
        convo.state = FINISHED
        session.flush()

    if verdict == THANKS:
        return Decision(
            REPLY,
            "Thanked the bot — replied and closed the conversation.",
            reply_text=load_flows().reply("thanks"),
        )

    # A bare "ok" means satisfied. Saying anything back is noise.
    return Decision(IGNORE, "Acknowledgement only — nothing to add.")


# ── Guided flows ───────────────────────────────────────────────────────────


def _active_conversation(session: Session, inbound: Inbound) -> Conversation | None:
    """The person's running flow, if it has not timed out."""
    if inbound.user_id is None:
        return None

    convo = session.scalar(
        select(Conversation)
        .where(
            Conversation.channel == inbound.channel,
            Conversation.chat_id == inbound.chat_id,
            Conversation.user_id == inbound.user_id,
            Conversation.state == ACTIVE,
        )
        .order_by(Conversation.id.desc())
    )
    if convo is None:
        return None

    timeout = timedelta(minutes=load_flows().timeout_minutes)
    last = convo.updated_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if utcnow() - last > timeout:
        # Never trap someone in a conversation they walked away from.
        convo.state = ABANDONED
        session.flush()
        return None

    return convo


def _finish(
    session: Session,
    inbound: Inbound,
    convo: Conversation,
    step: Step,
    flow: Flow,
    log_escalation: bool = True,
) -> Decision:
    """Deliver a terminal step and close the conversation."""
    convo.append("bot", step.prompt)
    convo.state = FINISHED
    convo.step_id = step.id

    decision = Decision(
        REPLY,
        f"Flow {flow.id!r} finished: {step.end}.",
        reply_text=step.prompt,
    )

    if step.end == ESCALATE and log_escalation:
        # The queue row carries everything already collected, so the admin
        # opens it knowing the region, the version, and what was tried.
        entries = convo.entries()
        opening = next(
            (e["text"] for e in entries if e.get("role") == "user"), inbound.text
        )
        query = Query(
            ref=next_ref(session, inbound.channel),
            channel=inbound.channel,
            tg_chat_id=inbound.chat_id,
            tg_message_id=inbound.message_id,
            tg_user_id=inbound.user_id,
            user_handle=inbound.user_handle,
            body=opening,
            received_at=utcnow(),
            state=UNANSWERED,
            reason=f"Guided flow “{flow.name}” finished without resolving it.",
            topic=flow.name,
            transcript=render_transcript(entries),
        )
        session.add(query)
        session.flush()
        for term in extract_candidate_keywords(opening)[:6]:
            session.add(QueryKeyword(query_id=query.id, term=term, is_new=False))
        session.flush()

        decision.query_ref = query.ref
        decision.query_id = query.id
        decision.reason = f"Guided flow “{flow.name}” finished without resolving it."

    session.flush()
    return decision


def _consult_kb(
    inbound: Inbound,
    session: Session,
    convo: Conversation,
    flow: Flow,
    judge: Judge,
    text: str,
) -> Decision | None:
    """Answer from the knowledge base mid-flow, or None to carry on the script.

    Rules still bind: a description that trips one is held for a human rather
    than answered, exactly as it would be outside a flow.
    """
    ctx = ChannelContext.load(session, inbound.channel)
    if ctx.tripped_rule(text) is not None:
        return None

    verdict = judge.judge(text, ctx.prompt_kb(), ctx.rule_texts())
    threshold = ctx.settings.reply_threshold or 0.62
    if not verdict.ok or not verdict.covered_by_kb or verdict.confidence < threshold:
        return None
    if not verdict.answer:
        return None

    convo.append("bot", verdict.answer)
    convo.state = FINISHED
    session.flush()
    return Decision(
        REPLY,
        f"Guided flow “{flow.name}”: the knowledge base covered what they "
        f"described ({verdict.confidence:.2f}).",
        reply_text=verdict.answer,
        verdict=verdict,
    )


def advance_flow(
    inbound: Inbound,
    session: Session,
    *,
    judge: Judge | None = None,
    log_escalation: bool = True,
) -> Decision | None:
    """Run the guided-flow layer. Returns None when no flow is involved.

    `log_escalation=False` runs the flow without filing a queue row, which is
    what the admin's rehearsal in a DM wants: the conversation still advances,
    but testing the script does not fill the real queue.
    """
    text = (inbound.text or "").strip()
    if inbound.from_bot or inbound.is_command or text.startswith("/"):
        return None
    if not text and not inbound.has_photo:
        return None

    flows = load_flows()
    if not flows.flows:
        return None

    convo = _active_conversation(session, inbound)

    # --- Already in a flow: this message answers the current step ----------
    if convo is not None:
        flow = flows.get(convo.flow_id)
        step = flow.step(convo.step_id) if flow else None
        if flow is None or step is None:
            # The definition changed under a running conversation.
            convo.state = ABANDONED
            session.flush()
            return None

        convo.append("user", text, photo=inbound.has_photo)

        # Some steps try the knowledge base against what was just described
        # before moving on — answering beats handing over.
        if step.consult_kb and judge is not None and text:
            answered = _consult_kb(inbound, session, convo, flow, judge, text)
            if answered is not None:
                return answered

        target_id = next_step_id(step, text, has_photo=inbound.has_photo)
        target = flow.step(target_id) if target_id else None
        if target is None:
            convo.state = FINISHED
            session.flush()
            return None

        if target.is_terminal:
            return _finish(
                session, inbound, convo, target, flow, log_escalation=log_escalation
            )

        convo.step_id = target.id
        convo.append("bot", target.prompt)
        session.flush()
        return Decision(
            REPLY,
            f"Guided flow “{flow.name}”: asked for {target.id}.",
            reply_text=target.prompt,
        )

    # --- Not in a flow: does this message start one? ----------------------
    flow = flows.match(text)
    if flow is None:
        return None

    first = flow.step(flow.first_step)
    if first is None:
        return None

    convo = Conversation(
        channel=inbound.channel,
        chat_id=inbound.chat_id,
        user_id=inbound.user_id,
        flow_id=flow.id,
        step_id=first.id,
        state=ACTIVE,
    )
    session.add(convo)
    convo.append("user", text, photo=inbound.has_photo)
    convo.append("bot", first.prompt)
    session.flush()

    return Decision(
        REPLY,
        f"Guided flow “{flow.name}” started — asking before answering.",
        reply_text=first.prompt,
    )
