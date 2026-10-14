"""The Jev client of one account's pass: stops asking Jev once it is clearly unusable."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from .classification import Classification
from .classifier import JevError, JevFailure, JevState, describe_jev_error, failure_kind

if TYPE_CHECKING:
    from .gmail import Email

log = logging.getLogger(__name__)

# A pass stops asking Jev after this many failed calls in a row (each one already retried by the
# SDK): Jev is down, and the rest of the emails that need it wait for the next pass instead of
# each spending a call, its retries and one of its own error attempts.
MAX_CONSECUTIVE_JEV_FAILURES = 3
# Jev refusing this many calls in a row the same way (same error type and HTTP status) is taken as
# a change on its side, not as bad emails: the pass stops asking Jev, and each refused email
# counts an ordinary failure (backoff) instead of giving up at once (ADR 0035).
MAX_IDENTICAL_REJECTIONS = 2


class Classifies(Protocol):
    """Anything that answers for one email's Jev state."""

    def classify(self, state: JevState) -> Classification: ...


class JevUnavailableError(Exception):
    """A pass stopped asking Jev (`JevGuard`); the message is the run's `runs.error`."""


class JevStoppedError(Exception):
    """Raised instead of asking Jev once the pass stopped doing so: the email isn't to blame and
    is left as it is for the next pass.

    Except on the call that stopped the pass by being refused like the one before it
    (`MAX_IDENTICAL_REJECTIONS`): `refusal` is that refusal, and its email counts an ordinary
    failure for it (`JevGuard.blame`).
    """

    def __init__(self, refusal: JevError | None = None) -> None:
        super().__init__()  # no args: the refusal's text can echo the email
        self.refusal = refusal


@dataclass(frozen=True)
class FailedEmail:
    """An email whose processing failed, to store as `error` (`triage._fail`).

    `alone`, for a Jev refusal (`JevFailure.REJECTED`): Jev refused this email alone, so it gives
    up at once; False when Jev refused every call the same way (an ordinary failure, retried
    after the backoff).
    """

    message_id: str
    failure: Exception
    email: Email | None  # as `triage.process` read it, for the error row
    alone: bool = True


class JevGuard:
    """The Jev client of one account's pass: stops calling Jev once it is clearly unusable.

    A failure Jev will repeat for every email (`JevFailure.CONFIG`: key revoked, no access,
    unknown model) stops it at once, and the email that hit it is not blamed (`JevStoppedError`).
    So do MAX_IDENTICAL_REJECTIONS refusals in a row with the same error type and HTTP status
    (`JevFailure.REJECTED`, e.g. two 422s): Jev refuses whatever it is sent, and the refused
    emails count an ordinary failure rather than giving up (`JevStoppedError.refusal`).
    Otherwise MAX_CONSECUTIVE_JEV_FAILURES failed calls in a row stop it; those failures are the
    emails' own (stored as `error`). A successful call resets both counts. Once stopped, every
    call raises `JevStoppedError` without reaching Jev; `stopped` says why.

    It also says, after each email, what the pass stores for a failure (`blame`): an email Jev
    refused is held back until the next call shows whether Jev refuses everything
    (`settle_held`, or `finish` at the pass's end), and an email left for the next pass because
    the guard had stopped is counted in `skipped`.
    """

    def __init__(
        self, client: Classifies, account: str, limit: int = MAX_CONSECUTIVE_JEV_FAILURES
    ) -> None:
        self._client, self._account, self._limit = client, account, limit
        self.consecutive_failures = 0
        self.stopped: str | None = None
        # The latest call's error when Jev refused it (REJECTED), else None; with the number of
        # identical refusals in a row it ends.
        self._last_rejection: JevError | None = None
        self._identical_rejections = 0
        self._held: FailedEmail | None = None  # an email Jev refused, until the next call says why
        self.skipped = 0  # emails left untouched for the next pass: they needed Jev once stopped

    def check(self) -> None:
        """Raise `JevStoppedError` if the pass no longer asks Jev (before work only Jev needs)."""
        if self.stopped is not None:
            raise JevStoppedError

    def classify(self, state: JevState) -> Classification:
        self.check()
        try:
            result = self._client.classify(state)
        except JevError as exc:
            failure = exc
        else:
            self.consecutive_failures = 0
            self._last_rejection, self._identical_rejections = None, 0
            return result
        # Outside the `except`, so nothing raised here chains the Jev error's text (see
        # triage._attempt).
        self.consecutive_failures += 1
        kind = failure_kind(failure)
        self._track_rejection(failure if kind is JevFailure.REJECTED else None)
        what = describe_jev_error(failure, request_id=False)
        if kind is JevFailure.CONFIG:
            self._stop(
                f"Jev rejected the API key or model ({what}): check TYPESAFE_API_KEY and "
                "TYPESAFE_MODEL; emails that need Jev wait for the next run"
            )
            raise JevStoppedError
        if self._identical_rejections >= MAX_IDENTICAL_REJECTIONS:
            self._stop(
                f"Jev refused {self._identical_rejections} requests in a row the same way "
                f"({what}): likely a change on Jev's side; emails that need Jev wait for the "
                "next run"
            )
            raise JevStoppedError(refusal=failure)
        if self.consecutive_failures >= self._limit:
            self._stop(
                f"Jev unavailable: {self.consecutive_failures} calls in a row failed (last: "
                f"{what}); emails that need Jev wait for the next run"
            )
        raise failure

    @property
    def refusing_everything(self) -> bool:
        """Stopped because Jev refused MAX_IDENTICAL_REJECTIONS calls in a row the same way."""
        return self._identical_rejections >= MAX_IDENTICAL_REJECTIONS

    def blame(self, message_id: str, failure: Exception, email: Email | None) -> FailedEmail | None:
        """What the pass stores for an email whose processing raised `failure`, or None for now.

        None when the email needed Jev after the guard stopped (left untouched, counted in
        `skipped`) or when it is this call's refusal (`_last_rejection`): held until the next
        call, or the pass's end, says whose fault it was (`settle_held`, `finish`). The call
        that stopped the guard by being refused like the one before it (`JevStoppedError.refusal`)
        is an ordinary failure. Anything else is the email's own failure.
        """
        if isinstance(failure, JevStoppedError):
            if failure.refusal is None:
                self.skipped += 1
                return None
            return FailedEmail(message_id, failure.refusal, email, alone=False)
        if failure is self._last_rejection:
            self._held = FailedEmail(message_id, failure, email)
            return None
        return FailedEmail(message_id, failure, email)

    def settle_held(self) -> FailedEmail | None:
        """The held refusal once a later Jev call has been made (else None), to store now.

        Called after each email: when Jev's latest call is no longer the held refusal, that
        call says whose fault it was (`_release`).
        """
        held = self._held
        if held is None or self._last_rejection is held.failure:
            return None
        return self._release(held)

    def finish(self) -> FailedEmail | None:
        """At the pass's end: the held refusal, if any. No later call was made, so as far as
        anyone knows Jev refused only this email."""
        return self._release(self._held) if self._held is not None else None

    def _release(self, held: FailedEmail) -> FailedEmail:
        """The held refusal, settled: an ordinary failure when Jev refuses every call the same
        way (`refusing_everything`), so two emails Jev really refuses can't stop every pass
        before the emails behind them; otherwise it was this email, which gives up at once."""
        self._held = None
        return replace(held, alone=not self.refusing_everything)

    def _track_rejection(self, rejection: JevError | None) -> None:
        """Count `rejection` (None: the call failed another way) toward identical refusals."""
        if rejection is None:
            self._identical_rejections = 0
        elif self._last_rejection is not None and _rejection_key(rejection) == _rejection_key(
            self._last_rejection
        ):
            self._identical_rejections += 1
        else:
            self._identical_rejections = 1
        self._last_rejection = rejection

    def _stop(self, reason: str) -> None:
        self.stopped = reason
        self._last_rejection = None
        log.error("stopped asking Jev", extra={"account": self._account, "reason": reason})


def _rejection_key(exc: JevError) -> tuple[type, int | None]:
    """What makes two refusals "the same way": the SDK error type and the HTTP status."""
    return type(exc), getattr(exc, "status", None)
