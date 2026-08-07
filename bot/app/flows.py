"""Guided diagnostic flows.

Pure logic: loading the definitions, deciding whether a message starts a flow,
and working out the next step. No database, no network — everything here is
directly testable.

The flow is executed exactly as the operator wrote it. The model is not asked
to improvise the questions or to decide the order, because a troubleshooting
script that skips a step is worse than no script at all.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import BOT_DIR
from app.kb import fold, match_keywords, normalise

FLOWS_PATH = BOT_DIR / "flows.yaml"
DEFAULT_TIMEOUT_MINUTES = 30

# How a flow finished.
RESOLVED = "resolved"
ESCALATE = "escalate"

# "ok" and "okay" are deliberately absent: they are acknowledgements, handled
# before the flow ever sees them. Reading "ok" as "yes" made the bot advance a
# step the person had not actually answered.
_YES = re.compile(
    r"^\W*(y|ye|yes|yeah|yep|yup|yh|sure|correct|i (have|did|am)|done|updated|"
    r"already|of course)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\W*(n|no|nope|nah|not yet|havent|haven't|have not|didnt|didn't|did not|"
    r"i haven'?t|i did ?n'?t|never)\b",
    re.IGNORECASE,
)


@dataclass
class Step:
    id: str
    #: A question put to the user; the flow then waits for their reply.
    ask: str | None = None
    #: A statement; the flow does not wait, it ends here.
    say: str | None = None
    next: str | None = None
    branches: list[dict] = field(default_factory=list)
    fallback: str | None = None
    #: "photo" when the step wants a screenshot.
    expects: str | None = None
    #: RESOLVED or ESCALATE when this step finishes the flow.
    end: str | None = None
    #: Try the knowledge base against what the person just said before moving
    #: on. Many descriptions ("it says handshake failed") are already
    #: documented, and answering beats handing over.
    consult_kb: bool = False

    @property
    def prompt(self) -> str:
        return (self.ask or self.say or "").strip()

    @property
    def is_terminal(self) -> bool:
        return self.end is not None


@dataclass
class Flow:
    id: str
    name: str
    triggers_any: list[str]
    triggers_require: list[str]
    first_step: str
    steps: dict[str, Step]

    def step(self, step_id: str) -> Step | None:
        return self.steps.get(step_id)

    def matches(self, text: str) -> bool:
        """Both halves must hit.

        `require` is what keeps "my phone is not working" from starting a VPN
        troubleshooting flow in a VPN support group.
        """
        if not text:
            return False
        if not match_keywords(text, self.triggers_any):
            return False
        if self.triggers_require and not match_keywords(text, self.triggers_require):
            return False
        return True


#: Used when flows.yaml has no `replies:` block of its own.
DEFAULT_REPLIES = {
    "thanks": (
        "You're welcome. If you have any other question or run into any issue "
        "using Creeb, I'll be glad to help — or you can reach an admin directly "
        "at @creebadminbot."
    ),
    "welcome": "",  # empty means greet nobody
}


def render_welcome(template: str, names: list[str]) -> str:
    """Fill {name} in the welcome copy.

    Uses replacement rather than str.format so an operator can write braces,
    percent signs or anything else in their own copy without it raising.
    """
    if not template.strip() or not names:
        return ""
    if len(names) == 1:
        who = names[0]
    elif len(names) == 2:
        who = f"{names[0]} and {names[1]}"
    else:
        who = ", ".join(names[:-1]) + f" and {names[-1]}"
    return template.replace("{name}", who).strip()


@dataclass
class FlowSet:
    flows: list[Flow]
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
    replies: dict = field(default_factory=lambda: dict(DEFAULT_REPLIES))

    def reply(self, name: str) -> str:
        return (self.replies.get(name) or DEFAULT_REPLIES.get(name, "")).strip()

    def match(self, text: str) -> Flow | None:
        for flow in self.flows:
            if flow.matches(text):
                return flow
        return None

    def get(self, flow_id: str) -> Flow | None:
        return next((f for f in self.flows if f.id == flow_id), None)


def load_flows(path: Path = FLOWS_PATH) -> FlowSet:
    if not path.is_file():
        return FlowSet(flows=[])

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    flows: list[Flow] = []

    for raw in data.get("flows") or []:
        steps = {}
        for raw_step in raw.get("steps") or []:
            step = Step(
                id=str(raw_step["id"]),
                ask=raw_step.get("ask"),
                say=raw_step.get("say"),
                next=raw_step.get("next"),
                branches=list(raw_step.get("branches") or []),
                fallback=raw_step.get("fallback"),
                expects=raw_step.get("expects"),
                end=raw_step.get("end"),
                consult_kb=bool(raw_step.get("consult_kb")),
            )
            steps[step.id] = step

        triggers = raw.get("triggers") or {}
        flows.append(
            Flow(
                id=str(raw["id"]),
                name=raw.get("name", raw["id"]),
                triggers_any=[str(t) for t in (triggers.get("any") or [])],
                triggers_require=[str(t) for t in (triggers.get("require") or [])],
                first_step=str(raw.get("first_step") or (raw.get("steps") or [{}])[0].get("id")),
                steps=steps,
            )
        )

    replies = dict(DEFAULT_REPLIES)
    replies.update(
        {str(k): str(v) for k, v in (data.get("replies") or {}).items() if v}
    )

    return FlowSet(
        flows=flows,
        timeout_minutes=int(data.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES),
        replies=replies,
    )


# --- Acknowledgements ------------------------------------------------------
#
# "Ok thank you" is someone closing the conversation, not answering the
# question they were last asked. Read as an answer it makes the bot advance a
# step nobody completed — which is exactly how a satisfied user ended up
# escalated to an admin.

#: Gratitude. These earn a friendly reply.
GRATITUDE = frozenset(
    """
    thanks thank thanx tanx tanks thankyou thanku thnx thx ty tysm
    appreciate appreciated appreciation grateful gracias merci
    """.split()
)

#: Plain acknowledgement. These earn silence — the person is satisfied and a
#: bot saying "you're welcome" to "ok" is noise in a busy group.
ACKNOWLEDGEMENT = frozenset(
    """
    ok okay oky okey okk k kk kkk alright aight
    cool nice great perfect fine noted gotcha understood
    """.split()
)

#: Words that carry no meaning of their own in these phrases, so they neither
#: make a message an acknowledgement nor stop it being one.
_ACK_FILLER = frozenset(
    """
    a an the you u yh so much very lot lots bro boss sir madam maam man men
    guys guy dear mate pal chief oga admin bot for it that all my me i
    now then well and but plus too also
    """.split()
)

_ACK_TOKEN = re.compile(r"[a-z0-9']+")

# What an acknowledgement means for the conversation.
THANKS = "thanks"
ACK = "ack"


def classify_acknowledgement(text: str) -> str | None:
    """THANKS, ACK, or None when the message says something substantive.

    A message counts only when it is *entirely* pleasantry. "thanks, how do I
    update?" is a question and must reach the normal pipeline; "ok thank you"
    is not.
    """
    body = fold(text)
    if not body:
        return None

    tokens = _ACK_TOKEN.findall(body)
    if not tokens:
        # Emoji or punctuation only — a thumbs-up is agreement, not a question.
        return ACK

    grateful = False
    for token in tokens:
        if token in GRATITUDE:
            grateful = True
        elif token not in ACKNOWLEDGEMENT and token not in _ACK_FILLER:
            return None  # something substantive is being said

    return THANKS if grateful else ACK


def yes_or_no(text: str) -> str | None:
    """Read a yes/no answer, or None when it is neither."""
    body = normalise(text)
    if not body:
        return None
    if _YES.match(body):
        return "yes"
    if _NO.match(body):
        return "no"
    return None


def next_step_id(step: Step, answer: str, has_photo: bool = False) -> str | None:
    """Where the flow goes after `answer` to `step`.

    Returns None when the definition has nowhere to go, which the caller treats
    as the end of the flow rather than a crash.
    """
    if step.branches:
        verdict = yes_or_no(answer)
        # A photo where one was asked for is an answer in itself.
        if verdict is None and has_photo:
            verdict = "yes"
        for branch in step.branches:
            if str(branch.get("when")).strip().lower() == verdict:
                return branch.get("next")
        return step.fallback or step.next

    return step.next


def render_transcript(entries: list[dict]) -> str:
    """The collected conversation, for the admin's queue row and DM."""
    lines = []
    for entry in entries:
        who = "Bot" if entry.get("role") == "bot" else "User"
        text = (entry.get("text") or "").strip()
        if entry.get("photo"):
            text = (text + " [screenshot attached]").strip()
        lines.append(f"{who}: {text}")
    return "\n".join(lines)
