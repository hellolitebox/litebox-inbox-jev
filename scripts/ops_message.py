"""Text for the host-side ops alerts (scripts/alert.sh, scripts/ops_common.sh).

Standard library only: it runs with the host's `python3 -I`, outside the app image, so an alert
still goes out when the image is broken.

    ops_message.py log-lines [N]    journal lines on stdin -> the last N app log lines, reduced
                                    to fields that can't carry email content
    ops_message.py payload CHANNEL  message text on stdin -> a chat.postMessage JSON body

Why only some fields: the app never logs bodies, and its own loggers' (`inbox_jev…`) `msg`
values are fixed strings, but other fields can carry what an email or an API said: `exc` (a
traceback, whose message may quote a Gmail or HTTP response), `sender`, `labels`, Jev's `error`,
and the `msg` of a third-party logger (googleapiclient's retry line quotes the request URL, with
its `q=to:someone@…` search). An alert keeps the time, level, `msg` (app loggers only; another
logger's line keeps only its name), `account`, `run_id`, the run counts, and the exception's
type (the `exc_type` the app's JsonFormatter writes), never its message; the full lines stay in
the journal on the server.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Sequence
from typing import TextIO

# Fields copied from a log line, after its time, level and msg, with the type each must have: a
# value of another type (free text where a count was expected) is dropped.
SAFE_FIELDS: dict[str, type] = {
    "account": str,
    "run_id": int,
    "processed": int,
    "errors": int,
    "failed": bool,
}
DEFAULT_LINES = 20
# The app's loggers: `inbox_jev` and its children. Only their `msg` is a fixed string.
APP_LOGGER = "inbox_jev"
# A (dotted) Python name: a logger name or an exception's qualified type.
_NAME = re.compile(r"[A-Za-z_][\w.]*")


def _name(value: object) -> str | None:
    """`value` when it is a dotted Python name, else None (never free text)."""
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def is_app_logger(name: object) -> bool:
    """Whether a log line's `logger` is the app's own (`inbox_jev` or `inbox_jev.…`)."""
    return isinstance(name, str) and (name == APP_LOGGER or name.startswith(APP_LOGGER + "."))


def summarize(line: str) -> str | None:
    """One JSON log line as `HH:MM:SS LEVEL msg key=value…`, or None for anything else
    (docker compose output, the CLI's plain-text stderr, malformed JSON).

    A third-party logger's line (googleapiclient, urllib3, typesafe_sdk…) keeps its level and
    logger name but not its `msg`, which formats arguments that can quote a request or a
    response."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    level, msg, logger = record.get("level"), record.get("msg"), record.get("logger")
    if not isinstance(level, str) or not isinstance(msg, str):
        return None
    if not is_app_logger(logger):
        msg = f"[{_name(logger) or '?'}] (message not forwarded)"
    ts = record.get("ts")
    parts = [ts[11:19] if isinstance(ts, str) else "--:--:--", level, msg]
    # The exact type: a bool isn't a count, nor a count a bool.
    parts += [
        f"{key}={record[key]}" for key, kind in SAFE_FIELDS.items() if type(record.get(key)) is kind
    ]
    if exc_type := _name(record.get("exc_type")):
        parts.append(f"error_type={exc_type}")
    return " ".join(parts)


def log_lines(lines: Iterable[str], limit: int = DEFAULT_LINES) -> list[str]:
    """The last `limit` app log lines among `lines`, summarized."""
    kept = [s for line in lines if (s := summarize(line.strip())) is not None]
    return kept[-limit:] if limit > 0 else []


def slack_escape(text: str) -> str:
    """Escape Slack's control characters, so text can't mention @channel or fake a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def payload(channel: str, text: str) -> str:
    """The chat.postMessage body: `text` escaped, link previews off."""
    return json.dumps(
        {
            "channel": channel,
            "text": slack_escape(text),
            "unfurl_links": False,
            "unfurl_media": False,
        }
    )


def main(argv: Sequence[str], stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    usage = "usage: ops_message.py log-lines [N] | payload CHANNEL"
    match list(argv):
        case ["log-lines"]:
            limit = DEFAULT_LINES
        case ["log-lines", count] if count.isdigit():
            limit = int(count)
        case ["payload", channel] if channel:
            stdout.write(payload(channel, stdin.read()) + "\n")
            return 0
        case _:
            print(usage, file=sys.stderr)
            return 2
    for line in log_lines(stdin, limit):
        stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
