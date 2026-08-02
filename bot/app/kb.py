"""Knowledge base assembly, the keyword gate, and the question heuristic.

Pure text work — no database, no network, no Telegram. Everything here is
directly unit-testable.
"""

import re

# Words carrying no signal as a discovered keyword. Deliberately short: this
# list only has to be good enough for a discovery feed you skim, not for search.
STOPWORDS = frozenset(
    """
    a about above actually after again all almost also always am an and another any anyone
    anybody are aren as ask asked asking asks at
    back bad be because been before being below best better between big bit both but by
    came can cant cannot come comes could couldnt
    did didnt do does doesnt doing done dont down during
    each either else even ever every everyone
    few first for from further
    get gets getting give given gives go goes going gone good got guy guys
    had hadnt has hasnt have havent having he hello help her here hers hey hi him his how however
    i if im in inside instead into is isnt it its itself ive
    just
    keep kind know
    let like long look looking looks lot lots
    made make makes many me more most much must my
    need needed needs new no nope nor not now
    of off often ok okay on once one only or other our out outside over own
    pls please put
    rather really right
    said same say says second see she should shouldnt simply since small so some still such sure
    take taken takes tell tells than thank thanks that thats the their them then there these
    they thing things this those though through to told too took try tried trying two
    under until up us use used using
    very
    want was wasnt way ways we well went were werent what when where which while who whom why
    will with without wont work worked working works would wouldnt
    yeah yep yes yet you your youre yours
    """.split()
)

# Message openers that mark something as a question even without a question mark.
QUESTION_OPENERS = frozenset(
    """
    how why when what where who which whose
    can could will would should shall may might
    is are was were do does did done
    has have had
    any anyone anybody somebody someone
    pls please help
    """.split()
)

# Words that mark a complaint. A complaint is worth capturing even when it is
# phrased as a statement rather than a question.
COMPLAINT_MARKERS = frozenset(
    """
    unacceptable terrible awful horrible useless broken rubbish nonsense
    scam scammer fraud fraudulent thief stealing stolen
    angry annoyed frustrated furious disgusted disappointed
    worst hate never again
    sue lawyer refund complaint complain
    still waiting nothing happened
    abeg wahala vex vexing spoil spoiled spoilt yawa palava
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9']+")

#: Bounds on anything stored as a keyword. The upper bound is not cosmetic —
#: `QueryKeyword.term` is String(64), and a pasted URL or a mashed-keyboard
#: "word" would otherwise be written straight into it.
MIN_TERM_LEN = 3
MAX_TERM_LEN = 32


def sane_term(term: str) -> str | None:
    """Normalise a candidate keyword, or None if it is not worth storing.

    Applied to model-supplied keywords as well as extracted ones, so nothing
    reaches the database without passing through here.
    """
    term = (term or "").strip().strip("'\"").lower()
    if not term or term.isdigit():
        return None
    if not (MIN_TERM_LEN <= len(term) <= MAX_TERM_LEN):
        return None
    if term in STOPWORDS:
        return None
    return term


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


#: Straight, curly and backtick apostrophes. Phone keyboards produce the curly
#: one by default, so "can't" arrives three different ways.
_APOSTROPHES = str.maketrans({"'": "", "’": "", "ʼ": "", "`": ""})


def fold(text: str) -> str:
    """Normalise, and drop apostrophes so "can't" and "cant" are one word."""
    return normalise(text).translate(_APOSTROPHES)


def _term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher tolerant of spacing inside multi-word terms."""
    parts = [re.escape(p) for p in fold(term).split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def match_keywords(text: str, terms: list[str]) -> list[str]:
    """Target terms present in `text`, in the order the terms were given.

    Matching is on word boundaries so "account" does not fire on "accountant"
    and "verify" does not fire on "diversify". Apostrophes are folded away on
    both sides, so a trigger written "cant connect" catches "can't connect"
    and "can’t connect" as well.
    """
    body = fold(text)
    return [term for term in terms if term.strip() and _term_pattern(term).search(body)]


def looks_like_question(text: str) -> bool:
    """Cheap heuristic for "worth capturing even though no keyword matched".

    This is what stops a pure keyword gate from hiding brand-new topics — the
    exact questions you most want to discover are the ones you have no keyword
    for yet. No model call, no cost.
    """
    body = normalise(text)
    if not body:
        return False
    if "?" in body:
        return True

    words = _WORD_RE.findall(body)
    if not words:
        return False
    if words[0] in QUESTION_OPENERS:
        return True
    # "does anyone know", "so how do i" — an opener just behind a filler word.
    if len(words) > 1 and words[1] in QUESTION_OPENERS and len(words[0]) <= 4:
        return True
    return bool(COMPLAINT_MARKERS.intersection(words))


def extract_candidate_keywords(text: str, limit: int = 8) -> list[str]:
    """Terms a message introduces, for the keyword-discovery feed.

    Rough by design: unigrams minus stopwords, first-seen order preserved. It
    feeds a list you skim and promote from, not an index anything queries.
    """
    seen: list[str] = []
    for word in _WORD_RE.findall(normalise(text)):
        term = sane_term(word)
        if term is None or term in seen:
            continue
        seen.append(term)
        if len(seen) >= limit:
            break
    return seen


def derive_keywords(
    text: str, limit: int = 25, min_count: int = 2, exclude: list[str] | None = None
) -> list[tuple[str, int]]:
    """Suggest target keywords from the knowledge base itself.

    The gate and the knowledge base have to agree or the bot goes mute: a
    perfect knowledge base about VPNs is never consulted if the keywords still
    say "payout". Rather than asking the operator to keep two lists in sync by
    hand, read the subjects out of the prose they already wrote.

    Frequency over the knowledge base, minus stopwords, plus phrases that recur.
    Returns (term, count) ranked by count, so the caller can show its working.
    """
    from collections import Counter

    skip = {t.strip().lower() for t in (exclude or [])}
    words = _WORD_RE.findall(normalise(text))

    unigrams: Counter[str] = Counter()
    for word in words:
        term = sane_term(word)
        if term and term not in skip:
            unigrams[term] += 1

    # Two-word phrases ("free trial", "kill switch") are often the thing users
    # actually type, and a single word from them can be too generic to gate on.
    bigrams: Counter[str] = Counter()
    for first, second in zip(words, words[1:]):
        a, b = sane_term(first), sane_term(second)
        if a and b:
            phrase = f"{a} {b}"
            if len(phrase) <= MAX_TERM_LEN and phrase not in skip:
                bigrams[phrase] += 1

    ranked = [(t, n) for t, n in unigrams.items() if n >= min_count]
    # A phrase has to beat its parts to earn a slot, otherwise it is noise.
    for phrase, n in bigrams.items():
        if n >= min_count:
            left, right = phrase.split()
            if n >= max(unigrams[left], unigrams[right]) * 0.6:
                ranked.append((phrase, n))

    ranked.sort(key=lambda pair: (-pair[1], pair[0]))
    return ranked[:limit]


ANSWER_CONTRACT = """\
You relay answers out of a knowledge base. You are not writing — you are \
quoting. Find the passage that answers the question and hand it over intact.

HOW TO PRODUCE `answer`
1. Locate every passage in the knowledge base that bears on the question. \
Check the whole document, not just the first heading that looks right.
2. If more than one passage answers it, take the FULLEST one — the version with \
the concrete steps, names and numbers — and fold in any detail the others add \
that it lacks. A short summary elsewhere in the document never overrides a \
detailed procedure. Prefer the passage a person could actually follow.
3. Copy that material into `answer` essentially word for word.
4. Delete only the parts that are irrelevant to what was asked.
5. Change nothing else. Do not rewrite, do not tighten, do not "improve".

Copying is the default. Rewording is a mistake, not a style choice.

WORKED EXAMPLE
If the knowledge base says:

    Once the config is updated perform these 3 steps
    Step 1: Open the server list and choose a server
    Step 2: Open the network list and choose a tweak with the free label
    Step 3: Turn mobile data on and press the flash button to start

then `answer` contains those three steps, written out, numbered, in order.

An answer like "update the config, pick a server and a tweak, then connect" is \
WRONG. It is shorter and it reads well, and it has thrown away every \
instruction the person actually needed. Do not do that.

WHAT MUST SURVIVE VERBATIM
Numbers, prices, durations, limits, server names, menu labels, button names, \
screen names, file names, settings, links, codes and commands. These are the \
parts people act on. A reworded one is a wrong one.

LENGTH
Length is not a problem. A missing step is. If the knowledge base answers in \
two hundred words, answer in two hundred words. Never compress a procedure \
into a description of a procedure.

FORMATTING
Keep line breaks. Keep numbered steps numbered, one per line. You may drop a \
heading that adds nothing. Add no greeting, no sign-off, no encouragement, no \
sales pitch, and do not open with "Sure" or "Of course" — begin with the \
answer itself.

RULES YOU MUST FOLLOW
- The knowledge base is the whole of what you know about this product. If it \
does not answer the question, set covered_by_kb to false and leave `answer` \
empty. Do not improvise, do not reason from general knowledge about similar \
products, do not guess a plausible number.
- Never state a fee, rate, limit, or processing time that is not written in the \
knowledge base.
- Never ask for a password, PIN, card number, or verification code.
- Set confidence to how well the knowledge base actually covers THIS question: \
1.0 when it answers it outright, around 0.5 when it is adjacent but does not \
settle it, near 0 when it says nothing relevant. Judge coverage, not how \
fluent your answer sounds.
- topic: three or four words naming what was asked.
- keywords: the specific terms this question is about, lowercase.
"""


def build_system_prompt(kb_text: str, rules: list[str]) -> str:
    """Assemble the system message: contract, then rules, then the KB itself."""
    blocks = [ANSWER_CONTRACT]

    if rules:
        listed = "\n".join(f"- {r}" for r in rules)
        blocks.append(
            "ADDITIONAL RULES FROM THE OPERATOR (these beat the knowledge base):\n"
            + listed
        )

    blocks.append("=== KNOWLEDGE BASE ===\n" + (kb_text.strip() or "(empty)"))
    return "\n\n".join(blocks)
