"""JSON-lines logging to stdout (picked up by journald / docker logs)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

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


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("googleapiclient", "httpx", "httpx2", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sdk = logging.getLogger("typesafe_sdk")
    if not any(isinstance(f, DropSdkSuccess) for f in sdk.filters):
        sdk.addFilter(DropSdkSuccess())
