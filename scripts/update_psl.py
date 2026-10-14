"""Refresh the vendored Public Suffix List, `src/inbox_jev/public_suffix_list.dat`.

Downloads https://publicsuffix.org/list/public_suffix_list.dat, checks that it is a whole list
(exactly one VERSION and one COMMIT header line in the publisher's formats, both sections,
parseable by `inbox_jev.psl`, the basic suffixes in it), writes it only when its VERSION is
newer than the vendored one, and prints `updated` or `unchanged` with its VERSION and COMMIT.
With `--check` it never writes and prints `newer` or `current`. A list that fails a check is
never written: the script exits non-zero.

`.github/workflows/update-psl.yml` runs `--check` weekly and opens (or updates) an issue when a
newer list exists; a session then runs this script without `--check` on a branch and opens a PR
(the "PSL lane" in CLAUDE.md): `uv run python scripts/update_psl.py`. The app itself never
fetches the list. Standard library plus `inbox_jev.psl` only, so `PYTHONPATH=src python3` runs
it without installing the package.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from inbox_jev.psl import PublicSuffixList

URL = "https://publicsuffix.org/list/public_suffix_list.dat"
DEST = Path(__file__).resolve().parents[1] / "src" / "inbox_jev" / "public_suffix_list.dat"
TIMEOUT_SECONDS = 60
# The list is ~330 KB; anything this large is not it.
MAX_BYTES = 5_000_000
# Far below the ~10,000 rules the list has: a truncated or wrong file fails here.
MIN_RULES = 5_000
SECTION_MARKERS = ("// ===BEGIN ICANN DOMAINS===", "// ===BEGIN PRIVATE DOMAINS===")
# Suffixes every real list has, from both sections and every rule kind the parser handles.
MUST_BE_SUFFIXES = ("com", "co.uk", "com.ar", "wa.gov.au", "github.io", "foo.ck")
MUST_BE_DOMAINS = ("example.com", "www.ck", "city.kawasaki.jp")
# The header values as publicsuffix.org writes them (`2026-10-07_07-28-19_UTC`, a full SHA-1).
Field = Literal["VERSION", "COMMIT"]
HEADER_FORMATS: dict[Field, re.Pattern[str]] = {
    "VERSION": re.compile(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d_UTC"),
    "COMMIT": re.compile(r"[0-9a-f]{40}"),
}


def fetch(url: str = URL) -> str:
    """The list's text, read with a timeout and a size cap."""
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
        body: bytes = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"{url} is over {MAX_BYTES} bytes: not the public suffix list")
    return body.decode("utf-8")


def header(text: str, field: Field) -> str:
    """The value of the list's `// VERSION: …` or `// COMMIT: …` header line, or ValueError.

    Strict, since the value goes into an issue the workflow opens: exactly one such line, and
    the value in the format the publisher uses (`HEADER_FORMATS`).
    """
    values = re.findall(rf"^// {field}: (.*)$", text, re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f"expected one {field} header line in the list, found {len(values)}")
    if not HEADER_FORMATS[field].fullmatch(values[0]):
        raise ValueError(f"malformed {field} header line in the list")
    return str(values[0])


def validate(text: str) -> tuple[str, str]:
    """(VERSION, COMMIT) of a whole, usable list, or ValueError saying what is wrong."""
    version, commit = header(text, "VERSION"), header(text, "COMMIT")
    missing = [m for m in SECTION_MARKERS if m not in text]
    if missing:
        raise ValueError(f"section markers missing: {', '.join(missing)}")
    rules = PublicSuffixList.parse(text)
    if len(rules.rules) < MIN_RULES:
        raise ValueError(f"only {len(rules.rules)} rules, expected at least {MIN_RULES}")
    wrong = [d for d in MUST_BE_SUFFIXES if rules.suffix_size(d.split(".")) != d.count(".") + 1]
    wrong += [d for d in MUST_BE_DOMAINS if rules.suffix_size(d.split(".")) == d.count(".") + 1]
    if wrong:
        raise ValueError(f"unexpected answers for: {', '.join(wrong)}")
    return version, commit


def is_newer(version: str, dest: Path) -> bool:
    """Whether `version` is later than the VERSION of the list at `dest` (or `dest` is missing).

    VERSION is a UTC timestamp written most significant field first, so its strings sort in
    time order.
    """
    if not dest.exists():
        return True
    return version > header(dest.read_text(encoding="utf-8"), "VERSION")


def write(text: str, dest: Path) -> None:
    """Write `text` to `dest` atomically."""
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)


def main(argv: list[str] | None = None, fetch: Callable[[], str] = fetch) -> int:
    parser = argparse.ArgumentParser(description="Refresh the vendored Public Suffix List.")
    parser.add_argument("--dest", type=Path, default=DEST, help="default: %(default)s")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether a newer valid list exists; never write the list",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        metavar="FILE",
        help="also append newer=, version= and commit= lines to FILE ($GITHUB_OUTPUT)",
    )
    args = parser.parse_args(argv)
    try:
        text = fetch()
        version, commit = validate(text)
        newer = is_newer(version, args.dest)
        if newer and not args.check:
            write(text, args.dest)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as out:
                out.write(f"newer={str(newer).lower()}\nversion={version}\ncommit={commit}\n")
    except (OSError, ValueError) as exc:  # network, file, decoding or validation
        print(f"public suffix list not updated: {exc}", file=sys.stderr)
        return 1
    if args.check:
        status = "newer" if newer else "current"
    else:
        status = "updated" if newer else "unchanged"
    print(f"{status}: VERSION {version} COMMIT {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
