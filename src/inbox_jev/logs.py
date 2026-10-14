"""JSON-lines logging to stdout (picked up by journald / docker logs)."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from .config import LogLevel

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


# The `runs` row of the triage pass in progress (`run_context`), stamped on its log lines.
_run_id: ContextVar[int | None] = ContextVar("run_id", default=None)


@contextmanager
def run_context(run_id: int) -> Iterator[None]:
    """Tag every log line emitted inside the block with `run_id` (`RunIdFilter`), the pass's
    `runs` row, so a message, Jev or Gmail line can be tied to its run."""
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)


class RunIdFilter(logging.Filter):
    """Adds `run_id` to records logged inside `run_context` (an explicit `extra` wins)."""

    def filter(self, record: logging.LogRecord) -> bool:
        run_id = _run_id.get()
        if run_id is not None and not hasattr(record, "run_id"):
            record.run_id = run_id
        return True


# typesafe_sdk logs one INFO line per successful request; keep its retry/error lines.
_SDK_SUCCESS_FMT = "%s %s <- %s in %.0fms (request %s)"


class DropSdkSuccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (
            record.levelno == logging.INFO
            and record.msg == _SDK_SUCCESS_FMT
            and isinstance(args, tuple)
            and len(args) == 5
            and isinstance(args[2], int)
            and args[2] < 400
        )


def setup(level: LogLevel = LogLevel.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.value)
    for noisy in ("googleapiclient", "httpx", "httpx2", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sdk = logging.getLogger("typesafe_sdk")
    # Its DEBUG lines carry the request body (the email) and TYPESAFE_LOG_LEVEL can turn them on:
    # never below INFO, whatever LOG_LEVEL says.
    sdk.setLevel(max(logging.INFO, root.level))
    if not any(isinstance(f, DropSdkSuccess) for f in sdk.filters):
        sdk.addFilter(DropSdkSuccess())
