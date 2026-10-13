"""Jev's answer for one email, as plain data.

No imports beyond the standard library, so the modules that only read an answer (`policy`,
`rules`) don't pull in the TypeSafe SDK or the Gmail client through `classifier`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Classification:
    category: str
    confidence: float
    probabilities: dict[str, float]
    needs_reply: float
    tags: dict[str, float] = field(default_factory=dict)
    request_id: str | None = None
