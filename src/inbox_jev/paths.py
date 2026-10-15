"""Where inbox-jev keeps its files, and the defaults it ships (ADR 0053).

The **home** holds `.env`, `accounts.yaml`, `secrets/` and `data/`; every relative path in the
settings is taken from it. It is `INBOX_JEV_HOME` when set (the Docker image sets `/app`), else
the current directory when it is an inbox-jev deployment or checkout (it has `accounts.yaml`, as an
image's `/app` does even without the variable, or the `pyproject.toml` of the `inbox-jev` project),
else `~/.config/inbox-jev` (`$XDG_CONFIG_HOME/inbox-jev`) for an installed copy run from anywhere.
Another project's `.env` never claims the home: inbox-jev would read its settings, write its own
secrets there and open a second database next to the scheduled one.
"""

from __future__ import annotations

import os
import tomllib
from importlib import resources
from pathlib import Path

HOME_ENV = "INBOX_JEV_HOME"
APP_DIR = "inbox-jev"
PROJECT_NAME = "inbox-jev"
# The file that marks the current directory as a deployment (a checkout also has PROJECT_NAME's
# pyproject.toml).
ACCOUNTS_MARKER = "accounts.yaml"
# Where the wheel ships the repo's default files (pyproject.toml, `force-include`).
BUNDLED_DIR = "defaults"


def home() -> Path:
    """The directory inbox-jev reads its configuration from and keeps its data in."""
    if value := os.environ.get(HOME_ENV, "").strip():
        return Path(value).expanduser()
    cwd = Path.cwd()
    if (cwd / ACCOUNTS_MARKER).exists() or _is_checkout(cwd):
        return cwd
    config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(config) if config else Path.home() / ".config") / APP_DIR


def _is_checkout(directory: Path) -> bool:
    """Whether `directory` holds this project's `pyproject.toml` (an unreadable one: no)."""
    try:
        project = tomllib.loads((directory / "pyproject.toml").read_text()).get("project")
    except (OSError, ValueError):  # missing or unreadable; TOMLDecodeError, UnicodeDecodeError
        return False
    return isinstance(project, dict) and project.get("name") == PROJECT_NAME


def bundled(name: str) -> Path:
    """A default file the package ships: `categories.yaml`, `accounts.example.yaml` or
    `.env.example`. In the wheel it is package data; in a checkout (an editable install, where
    package data isn't copied) it is the repo's own file."""
    packaged = resources.files("inbox_jev") / BUNDLED_DIR / name
    if packaged.is_file():
        return Path(str(packaged))
    return Path(__file__).resolve().parents[2] / name
