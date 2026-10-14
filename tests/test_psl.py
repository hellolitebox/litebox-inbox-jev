"""The Public Suffix List parser (`inbox_jev.psl`) and the list shipped with the package."""

import re
from importlib.resources import files
from pathlib import Path

import pytest

from inbox_jev import psl
from inbox_jev.psl import PublicSuffixList, is_public_suffix, registrable_domain, to_ascii

# A small list in the PSL text format, covering every rule kind.
SAMPLE = """\
// ===BEGIN ICANN DOMAINS===
// uk : comment lines and blank lines are skipped

uk
co.uk
*.ck
!www.ck
jp
kawasaki.jp
*.kawasaki.jp
!city.kawasaki.jp
香港
公司.香港
// ===END ICANN DOMAINS===
// ===BEGIN PRIVATE DOMAINS===
github.io   trailing text after the rule is ignored
// ===END PRIVATE DOMAINS===
"""


@pytest.fixture(scope="module")
def sample():
    return PublicSuffixList.parse(SAMPLE)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Example.COM.", "example.com"),
        (" bücher.example ", "xn--bcher-kva.example"),
        ("公司.香港", "xn--55qx5d.xn--j6w193g"),
        ("XN--55QX5D.xn--j6w193g", "xn--55qx5d.xn--j6w193g"),  # already an A-label
        ("", ""),
    ],
)
def test_to_ascii(name, expected):
    assert to_ascii(name) == expected


@pytest.mark.parametrize("label", ["個人", "香港", "公司", "bücher", "рф"])
def test_to_ascii_agrees_with_idna_on_the_lists_labels(label):
    """Plain punycode of an NFC lowercase label is its IDNA A-label."""
    assert to_ascii(label) == label.encode("idna").decode("ascii")


def test_parse_reads_every_rule_kind(sample):
    assert sample.rules == {
        "uk",
        "co.uk",
        "jp",
        "kawasaki.jp",
        "xn--j6w193g",
        "xn--55qx5d.xn--j6w193g",
        "github.io",
    }
    assert sample.wildcards == {"ck", "kawasaki.jp"}
    assert sample.exceptions == {"www.ck", "city.kawasaki.jp"}


@pytest.mark.parametrize("text", ["", "// only comments\n\n", "   \n"])
def test_parse_refuses_a_list_without_rules(text):
    with pytest.raises(ValueError, match="no public suffix rules"):
        PublicSuffixList.parse(text)


@pytest.mark.parametrize(
    ("host", "size"),
    [
        ("example.co.uk", 2),  # the longest matching rule
        ("co.uk", 2),
        ("example.uk", 1),
        ("example.com", 1),  # no rule: the default "*", the TLD alone
        ("internal", 1),
        ("foo.ck", 2),  # a wildcard matches any one label
        ("a.foo.ck", 2),
        ("ck", 1),  # the wildcard's parent itself isn't matched by it
        ("www.ck", 1),  # an exception: the rule without its leftmost label
        ("a.www.ck", 1),
        ("b.kawasaki.jp", 3),  # wildcard wins over the shorter kawasaki.jp
        ("x.b.kawasaki.jp", 3),
        ("city.kawasaki.jp", 2),  # the exception wins over the wildcard
        ("x.city.kawasaki.jp", 2),
        ("acme.公司.香港", 2),  # Unicode input
        ("acme.xn--55qx5d.xn--j6w193g", 2),  # the same in punycode
        ("x.github.io", 2),  # PRIVATE rules count too
    ],
)
def test_suffix_size(sample, host, size):
    assert sample.suffix_size(to_ascii(host).split(".")) == size


def test_bundled_list_is_parsed_once():
    assert psl.bundled() is psl.bundled()


def test_bundled_list_ships_with_the_package_and_names_its_version():
    """The vendored file is package data (hatchling ships every file under src/inbox_jev in the
    wheel) and keeps the header scripts/update_psl.py reads."""
    text = files("inbox_jev").joinpath(psl.DATA_FILE).read_text(encoding="utf-8")
    assert re.search(r"^// VERSION: \d{4}-\d\d-\d\d_\d\d-\d\d-\d\d_UTC$", text, re.MULTILINE)
    assert re.search(r"^// COMMIT: [0-9a-f]{40}$", text, re.MULTILINE)
    assert "// ===BEGIN ICANN DOMAINS===" in text
    assert "// ===BEGIN PRIVATE DOMAINS===" in text


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("bounce.linkedin.co.uk", "linkedin.co.uk"),
        ("reply.com.ar", "reply.com.ar"),
        ("kim.reply.wa.gov.au", "reply.wa.gov.au"),  # a three-level ICANN suffix
        ("x.reply.blogspot.com", "reply.blogspot.com"),  # a PRIVATE suffix
        ("bounce.sap.de", "sap.de"),  # a short name under a ccTLD is a name
        ("mail.litebox.ai", "litebox.ai"),
        ("acme.公司.香港", "acme.xn--55qx5d.xn--j6w193g"),
        ("example.internal", "example.internal"),  # an unlisted TLD
        ("co.uk", None),
        ("wa.gov.au", None),
        ("github.io", None),
        ("com", None),
        ("", None),
        # An empty label makes no domain (the upstream vectors' "Leading dot" cases).
        (".com", None),
        (".example.com", None),
        ("a..com", None),
        ("bounce..linkedin.co.uk", None),
    ],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        # Named ccTLD second levels, and those of ccTLDs sold as generic names (ADR 0033's).
        ("co.uk", True),
        ("gouv.fr", True),
        ("nhs.uk", True),
        ("plc.uk", True),
        ("com.ar", True),
        ("com.ai", True),
        ("off.ai", True),
        ("com.co", True),
        ("org.co", True),
        ("com.io", True),
        ("org.io", True),
        ("gov.io", True),
        ("co.io", True),
        ("its.me", True),
        ("acme.co.uk", False),
        ("acme.io", False),
        ("sap.de", False),
        ("org.com", False),
        ("web.co", False),  # dropped from the PSL by the .co registry (2024-11-18)
        ("adv.ai", False),
        ("sch.ai", False),
        ("plc.io", False),
        ("com.tv", False),
        ("litebox.ai", False),
        # What the old name tables missed.
        ("wa.gov.au", True),
        ("kommune.no", True),
        ("plc.ly", True),
        ("co.gg", True),
        ("sch.gg", False),
        ("blogspot.com", True),  # PRIVATE section
        ("github.io", True),
        ("xn--55qx5d.xn--j6w193g", True),  # 公司.香港
        ("公司.香港", True),
        ("xn--p1ai", True),  # .рф itself
        ("xn--p1ai.xn--p1ai", False),
    ],
)
def test_is_public_suffix(domain, expected):
    assert is_public_suffix(domain) is expected


# The Public Suffix List project's own test vectors: `tests/test_psl.txt` of
# https://github.com/publicsuffix/list at commit 3929462652695bad04f0a27afb600974014a3c8b (the
# commit the vendored list was published from; the file last changed in 6ca11f5), vendored
# byte for byte as tests/vectors/test_psl.txt. Its header dedicates it to the public domain (CC0).
# Each `checkPublicSuffix(input, expected)` line is one case; `null` is None.
VECTORS_FILE = Path(__file__).parent / "vectors" / "test_psl.txt"
_VECTOR = re.compile(r"^checkPublicSuffix\((null|'[^']*'), (null|'[^']*')\);$")


def _vectors() -> list:
    cases = []
    for line in VECTORS_FILE.read_text(encoding="utf-8").splitlines():
        match = _VECTOR.match(line)
        if not match:
            assert not line.startswith("checkPublicSuffix"), f"unparsed vector: {line}"
            continue
        host, expected = (None if v == "null" else v[1:-1] for v in match.groups())
        marks = [pytest.mark.skip(reason="registrable_domain takes a str")] if host is None else []
        cases.append(pytest.param(host, expected, id=repr(host), marks=marks))
    return cases


VECTORS = _vectors()


def test_every_upstream_vector_is_read():
    assert len(VECTORS) == 78


@pytest.mark.parametrize(("host", "expected"), VECTORS)
def test_upstream_vector(host, expected):
    """The registrable domain the upstream vectors expect, compared as lowercase ASCII (ours
    returns IDN labels as their A-labels, the vectors keep the input's form)."""
    assert registrable_domain(host) == (None if expected is None else to_ascii(expected))
