"""Owner-only files, written atomically: OAuth tokens, the OAuth client, `.env`."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def write_private(path: Path, data: str) -> None:
    """Write `data` to `path` atomically, readable by its owner only from the start.

    The text goes to a temporary file in the same directory, created with mode 0600
    (`tempfile.mkstemp`), is flushed to disk and then renamed over `path`. A crash or a full
    disk leaves the previous file whole (never a truncated one), and the secret is never
    readable by others, not even briefly. The temporary file is removed if anything fails.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Persist a rename in `directory` (POSIX), best effort.

    The rename has already happened, so this only makes it durable sooner: where a directory
    can't be opened, or can't be fsynced (EINVAL on some filesystems), it is skipped.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.debug("fsync of %s skipped: %s", directory, exc)
    finally:
        os.close(fd)
