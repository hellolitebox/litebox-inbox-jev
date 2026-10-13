"""docs/decisions/README.md lists every ADR file, once, in order, and each ADR has its sections."""

import re
from pathlib import Path

import pytest

DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
ADR_FILES = sorted(DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
SECTIONS = ("## Context", "## Decision", "## Alternatives considered", "## Consequences")


def test_adr_index_matches_files():
    index = (DECISIONS / "README.md").read_text()
    rows = re.findall(r"^\| \[(\d{4})\]\((\d{4}-[\w-]+\.md)\) \|", index, flags=re.MULTILINE)
    assert ADR_FILES, "no ADR files found"
    assert [name for _, name in rows] == [p.name for p in ADR_FILES], (
        "the index must list every docs/decisions/NNNN-*.md file, once, in order"
    )
    assert all(number == name[:4] for number, name in rows), "a row's number must match its file"
    numbers = [int(p.name[:4]) for p in ADR_FILES]
    assert numbers == list(range(1, len(numbers) + 1)), "ADR numbers run from 0001 with no gaps"


@pytest.mark.parametrize("path", ADR_FILES, ids=lambda p: p.name[:4])
def test_adr_has_status_and_sections(path):
    text = path.read_text()
    assert re.search(r"^- Status: (Accepted|Superseded by \d{4})", text, flags=re.MULTILINE), (
        f"{path.name}: needs '- Status: Accepted' or '- Status: Superseded by NNNN'"
    )
    missing = [section for section in SECTIONS if section not in text]
    assert not missing, f"{path.name} lacks {missing}"
