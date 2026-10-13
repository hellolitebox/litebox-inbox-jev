"""Classify an email with TypeSafe Jev: one Choice for the category plus Noul yes/no questions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from typesafe_sdk import (
    Choice,
    Noul,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeClient,
    TypeSafeError,
)

from .config import CategoriesConfig
from .gmail import Email
from .signals import Signals

CATEGORY_Q = "category"
NEEDS_REPLY_Q = "needs_reply"
TAG_PREFIX = "tag_"

# What a failed Jev call raises (API, connection, invalid answer). Callers that can do without
# Jev's answer catch this, not Exception.
JevError = TypeSafeError


class JevNotConfigured(RuntimeError):
    """TYPESAFE_API_KEY is missing or malformed: no Jev call can work, so `run` doesn't start."""


class JevFailure(StrEnum):
    """What a failed Jev call says about the next calls (`failure_kind`)."""

    # 401, 403, 404: the API key is revoked or lacks access, or the model doesn't exist. Every
    # call fails the same way until the configuration changes, whatever the email.
    CONFIG = "config"
    # Any other 4xx except 408 and 429 (400, 422…): Jev refused this request, and the same email
    # would be refused again.
    REJECTED = "rejected"
    # Timeouts, connection errors, 408, 429, 5xx, an answer the SDK can't read and any other SDK
    # error: Jev may answer on a later try.
    TRANSIENT = "transient"


_CONFIG_STATUSES = frozenset({401, 403, 404})
_TRANSIENT_4XX = frozenset({408, 429})

# The SDK's own messages for a plain `TypeSafeError` raised before any request is sent (typesafe_sdk
# `_core/questions.py`, `endpoints.py`, `transport.py`): kept by `describe_jev_error` because they
# never contain the state, only a question name (our keys from categories.yaml) or nothing, and
# they are the only detail that says which question is wrong. The whole message must match, and a
# question name only matches as a short identifier, so a message carrying anything else (a future
# SDK message, an email) is reduced to the type name.
_QUESTION = r'"[A-Za-z0-9_-]{1,64}"'
_SAFE_SDK_MESSAGE = re.compile(
    "|".join(
        [
            r"At least one question is required\.",
            rf"Question {_QUESTION} must be a question object or a dictionary with a nonempty "
            r'string "type"\.',
            rf'Question {_QUESTION} requires "criteria"\.',
            rf"Score question {_QUESTION} has no criteria; at least one score is required\.",
            rf"Choice question {_QUESTION} has no criteria; at least one choice is required\.",
            rf"Noul question {_QUESTION} criteria must be an object\.",
            rf"Noul question {_QUESTION} has neither instructions nor criteria; at least one is "
            r"required\.",
            r"State is required\.",
            r"The request body could not be encoded as JSON",
        ]
    )
)


def failure_kind(exc: JevError) -> JevFailure:
    """Classify a failed Jev call (the SDK has already retried what it retries: 408, 429, 5xx,
    timeouts and connection errors)."""
    if isinstance(exc, TypeSafeAPIError) and not isinstance(
        exc, TypeSafeAPIResponseValidationError
    ):
        if exc.status in _CONFIG_STATUSES:
            return JevFailure.CONFIG
        if 400 <= exc.status < 500 and exc.status not in _TRANSIENT_4XX:
            return JevFailure.REJECTED
    return JevFailure.TRANSIENT


def describe_jev_error(exc: JevError, *, request_id: bool = True) -> str:
    """A failed Jev call as safe to store and log: type, HTTP status, request id.

    Never the SDK's message: for an HTTP error it is taken from the response body, which on a
    400/422 can echo the request, i.e. the email. A connection error's text (redacted by the SDK,
    no response exists), a validation error's field path and the SDK's own client-side messages
    (`_SAFE_SDK_MESSAGE`) are kept. `request_id=False` gives the same text for the same failure,
    so the digest can group identical failed runs.
    """
    name = type(exc).__name__
    if isinstance(exc, TypeSafeAPIError):
        parts = [f"HTTP {exc.status}"]
        if isinstance(exc, TypeSafeAPIResponseValidationError):
            parts.append(f"invalid answer at {exc.field_path!r}")
        if request_id and exc.request_id:
            parts.append(f"request_id={exc.request_id}")
        return f"{name} ({', '.join(parts)})"
    if isinstance(exc, TypeSafeAPIConnectionError):
        return f"{name}: {exc}"[:300]
    if type(exc) is TypeSafeError and _SAFE_SDK_MESSAGE.fullmatch(str(exc)):
        return f"{name}: {exc}"
    return name


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
            raise JevNotConfigured("TYPESAFE_API_KEY is not set: add it to .env")
        try:
            self._client = TypeSafeClient(api_key=api_key, model=model)
        except TypeSafeError as exc:  # a malformed key; the SDK's message never quotes it
            raise JevNotConfigured(
                f"TYPESAFE_API_KEY or TYPESAFE_MODEL is invalid: {exc}"
            ) from None
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
