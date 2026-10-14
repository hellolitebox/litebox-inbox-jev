"""Jev's answer for one email, and the sender relationship it is asked with, as plain data.

No imports beyond the standard library, so the modules that only read an answer (`policy`,
`rules`) don't pull in the TypeSafe SDK or the Gmail client through `classifier`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PreviousContact(StrEnum):
    """Whether I have written to a sender (`triage.previous_contact`'s answer).

    The values are what Jev's state carries (`previous_contact`) and what the run log records.
    """

    WROTE_TO_SENDER = "wrote_to_sender"
    WROTE_TO_THEIR_DOMAIN = "wrote_to_their_domain"
    NEVER = "never"


@dataclass
class Classification:
    category: str
    confidence: float
    probabilities: dict[str, float]
    needs_reply: float
    tags: dict[str, float] = field(default_factory=dict)
    request_id: str | None = None
