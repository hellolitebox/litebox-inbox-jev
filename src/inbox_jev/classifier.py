"""Classify an email with TypeSafe Jev: one Choice for the category plus Noul yes/no questions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from typesafe_sdk import Choice, Noul, TypeSafeClient, TypeSafeError

from .config import CategoriesConfig
from .gmail import Email
from .signals import Signals

CATEGORY_Q = "category"
NEEDS_REPLY_Q = "needs_reply"
TAG_PREFIX = "tag_"

CATEGORY_INSTRUCTIONS = (
    "Classify this email received in my work inbox into exactly one category. "
    "Judge by who wrote it and why, not only by the topic. "
    "previous_contact says whether I have ever sent an email to this sender (wrote_to_sender), "
    "to someone else at their company domain (wrote_to_their_domain) or never (never); it is "
    "missing when unknown. A personal-looking pitch from a sender I never wrote to is usually "
    "cold outreach, not important."
)


@dataclass
class Classification:
    category: str
    confidence: float
    probabilities: dict[str, float]
    needs_reply: float
    tags: dict[str, float] = field(default_factory=dict)
    request_id: str | None = None


def build_state(
    email: Email,
    account_email: str,
    body_chars: int,
    previous_contact: str | None = None,
    sig: Signals | None = None,
) -> dict:
    """`sig` is triage.signals_for's answer; defaults to the raw (unresolved) signals."""
    sig = sig or email.signals
    state = {
        "recipient": account_email,
        "from": email.header("From"),
        "to": email.header("To"),
        "cc": email.header("Cc"),
        "subject": email.header("Subject"),
        "date": datetime.fromtimestamp(email.internal_date_ms / 1000, UTC).isoformat(),
        "is_mailing_list": bool(sig.list_id or sig.has_unsubscribe),
        "precedence": email.header("Precedence") or None,
        "body": (email.body or email.snippet)[:body_chars],
    }
    if sig.relayed_by:
        # Sent to a group I'm in; "from" shows the group, this is who actually wrote it.
        state["original_sender"] = sig.sender
        state["sent_via_group"] = sig.relayed_by
    if previous_contact is not None:
        state["previous_contact"] = previous_contact
    return state


def build_questions(cfg: CategoriesConfig) -> dict:
    questions: dict = {
        CATEGORY_Q: Choice(
            instructions=CATEGORY_INSTRUCTIONS,
            criteria={c.key: c.criteria for c in cfg.categories},
        ),
        NEEDS_REPLY_Q: Noul(instructions=cfg.needs_reply.question),
    }
    for tag in cfg.tags:
        questions[TAG_PREFIX + tag.key] = Noul(instructions=tag.question)
    return questions


def _request_id(result) -> str | None:
    try:
        return result.request_id
    except TypeSafeError:  # raised when the x-typesafe-request-id header is missing
        return None


class Classifier:
    def __init__(self, api_key: str, model: str, cfg: CategoriesConfig):
        if not api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self._client = TypeSafeClient(api_key=api_key, model=model)
        self._cfg = cfg
        self._questions = build_questions(cfg)

    def classify(self, state: dict) -> Classification:
        result = self._client.system_one(state, self._questions)
        choice = result.choices[CATEGORY_Q]
        return Classification(
            category=choice.choice,
            confidence=choice.confidence,
            probabilities=dict(choice.probabilities),
            needs_reply=result.nouls[NEEDS_REPLY_Q].noul,
            tags={t.key: result.nouls[TAG_PREFIX + t.key].noul for t in self._cfg.tags},
            request_id=_request_id(result),
        )

    def close(self) -> None:
        self._client.close()
