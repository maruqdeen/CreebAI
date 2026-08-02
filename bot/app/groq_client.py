"""The single Groq call.

Uses strict `json_schema` structured output, which on Groq is supported only by
`openai/gpt-oss-120b` and `openai/gpt-oss-20b`. Every other Groq model falls
back to `json_object` — syntactically valid JSON with no schema enforcement.
The escalation decision rests on a typed verdict, so strict mode is the
mechanism here, not a nicety.

  https://console.groq.com/docs/structured-outputs

Note also that `llama-3.3-70b-versatile` shuts down on 2026-08-16.

Failure policy: **fail closed**. A timeout, an API error, or an unparseable
response all produce a verdict that escalates to a human. The bot going quiet
is a small cost; the bot inventing an answer about someone's money is not.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from app.config import settings
from app.kb import build_system_prompt, sane_term

log = logging.getLogger(__name__)

STRICT_SCHEMA_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-safeguard-20b")

VERDICT_SCHEMA = {
    "name": "support_verdict",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer", "confidence", "covered_by_kb", "topic", "keywords"],
        "properties": {
            "answer": {
                "type": "string",
                "description": "The reply to post, or an empty string when not covered.",
            },
            "confidence": {
                "type": "number",
                "description": "0-1. How well the knowledge base covers this question.",
            },
            "covered_by_kb": {
                "type": "boolean",
                "description": "True only if the knowledge base actually answers it.",
            },
            "topic": {
                "type": "string",
                "description": "Three or four words naming what was asked.",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific lowercase terms this question is about.",
            },
        },
    },
}


@dataclass(frozen=True)
class Verdict:
    answer: str
    confidence: float
    covered_by_kb: bool
    topic: str
    keywords: list[str]
    #: Set when the call itself failed, rather than the model declining.
    error: str | None = None
    latency_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def failed(cls, error: str, latency_ms: int | None = None) -> "Verdict":
        """A verdict that always escalates. Used for every call-level failure."""
        return cls(
            answer="",
            confidence=0.0,
            covered_by_kb=False,
            topic="",
            keywords=[],
            error=error,
            latency_ms=latency_ms,
        )


class Judge(Protocol):
    """What the pipeline needs. Tests supply their own; nothing else is used."""

    def judge(self, message: str, kb_text: str, rules: list[str]) -> Verdict: ...


class GroqJudge:
    """Groq-backed implementation of `Judge`."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.model = model or settings.groq_model
        self._api_key = api_key or settings.groq_api_key
        self._client = None

        if self.model not in STRICT_SCHEMA_MODELS:
            log.warning(
                "Model %r does not support strict json_schema on Groq; the verdict "
                "will not be schema-enforced. Supported: %s",
                self.model,
                ", ".join(STRICT_SCHEMA_MODELS),
            )

    @property
    def client(self):
        # Built lazily so importing this module never requires a key.
        if self._client is None:
            from groq import Groq

            self._client = Groq(
                api_key=self._api_key, timeout=settings.groq_timeout_seconds
            )
        return self._client

    def judge(self, message: str, kb_text: str, rules: list[str]) -> Verdict:
        if not self._api_key:
            return Verdict.failed("GROQ_API_KEY is not set")

        started = time.monotonic()
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": build_system_prompt(kb_text, rules)},
                    {"role": "user", "content": message},
                ],
                response_format={"type": "json_schema", "json_schema": VERDICT_SCHEMA},
                # Low, because the job is to reproduce the knowledge base
                # faithfully rather than to write something new.
                temperature=0.1,
                # Generous: answers now carry whole procedures, and a verdict
                # cut off mid-JSON is a verdict thrown away.
                max_tokens=2400,
            )
            elapsed = int((time.monotonic() - started) * 1000)
            raw = completion.choices[0].message.content or ""
        except Exception as exc:  # network, auth, rate limit, timeout
            elapsed = int((time.monotonic() - started) * 1000)
            log.error("Groq call failed: %s: %s", type(exc).__name__, exc)
            return Verdict.failed(f"{type(exc).__name__}: {exc}", elapsed)

        return parse_verdict(raw, latency_ms=elapsed)


def parse_verdict(raw: str, latency_ms: int | None = None) -> Verdict:
    """Turn the model's JSON into a Verdict, defensively.

    Strict mode makes malformed output very unlikely, but "very unlikely" is
    not "impossible" and the failure mode would be an exception inside the
    message handler. Anything unreadable escalates instead.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        log.error("Groq returned unparseable JSON: %s", exc)
        return Verdict.failed(f"unparseable response: {exc}", latency_ms)

    if not isinstance(data, dict):
        return Verdict.failed("response was not a JSON object", latency_ms)

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    raw_keywords = data.get("keywords") or []
    if not isinstance(raw_keywords, list):
        raw_keywords = []
    # Sanitised at the boundary, so nothing downstream has to think about what
    # the model might have put in this list.
    keywords: list[str] = []
    for k in raw_keywords:
        term = sane_term(str(k))
        if term and term not in keywords:
            keywords.append(term)

    return Verdict(
        answer=str(data.get("answer") or "").strip(),
        confidence=max(0.0, min(1.0, confidence)),
        covered_by_kb=bool(data.get("covered_by_kb")),
        topic=str(data.get("topic") or "").strip()[:255],
        keywords=keywords,
        latency_ms=latency_ms,
    )
