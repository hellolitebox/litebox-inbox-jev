"""inbox-jev: triage Gmail with TypeSafe Jev."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The one version source is pyproject.toml, read from the installed distribution's metadata
    # (the Docker image installs it non-editable; `uv sync` installs it editable for dev).
    __version__ = version("inbox-jev")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "unknown"
