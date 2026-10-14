"""The Public Suffix List (https://publicsuffix.org): which names nobody owns as a whole.

A public suffix is a name under which anyone can get a domain: registered from a registry
(`com`, `co.uk`, `wa.gov.au`, the list's ICANN section) or created on a shared platform
(`github.io`, `blogspot.com`, its PRIVATE section). The registrable domain of a host is its
public suffix plus one label (`linkedin.co.uk` for `bounce.linkedin.co.uk`).

Both sections count, for both uses (ADR 0037): a PRIVATE suffix is as public as an ICANN one
for trusting a `group_domains` entry (anyone can have `x.github.io`), and taking it as a suffix
for machine hosts only makes the registrable domain longer, so fewer hosts are machines (the
safe side).

The list ships with the package (`public_suffix_list.dat`, refreshed by
`scripts/update_psl.py`), is read once on first use and is never fetched at runtime. Rules and
names are compared as lowercase ASCII: a Unicode label becomes its punycode A-label
(`香港` → `xn--j6w193g`), so Unicode and `xn--` input give the same answer.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

DATA_FILE = "public_suffix_list.dat"


def to_ascii(name: str) -> str:
    """`name` lowercased, without a trailing dot, each non-ASCII label as its punycode A-label
    (`bücher.example` → `xn--bcher-kva.example`). ASCII labels, `xn--` ones included, are kept.

    Plain punycode of the NFC form, without IDNA's mapping or validation: the list's rules are
    already lowercase NFC, and an invalid name can only fail to match a rule.
    """
    labels = unicodedata.normalize("NFC", name.strip().lower().rstrip(".")).split(".")
    return ".".join(
        label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii")
        for label in labels
    )


@dataclass(frozen=True)
class PublicSuffixList:
    """The list's rules, as lowercase ASCII names (see `to_ascii`)."""

    rules: frozenset[str]  # `co.uk`
    wildcards: frozenset[str]  # `*.ck` stored as `ck`: every child of it is a suffix
    exceptions: frozenset[str]  # `!www.ck` stored as `www.ck`: not a suffix despite a wildcard

    @classmethod
    def parse(cls, text: str) -> PublicSuffixList:
        """Rules from the list's text format: one rule per line, the first whitespace-separated
        token; `//` comments and blank lines are skipped. Both sections are read.

        Raises ValueError when the text holds no rule at all.
        """
        rules: set[str] = set()
        wildcards: set[str] = set()
        exceptions: set[str] = set()
        for line in text.splitlines():
            tokens = line.split(maxsplit=1)
            if not tokens or tokens[0].startswith("//"):
                continue
            rule = tokens[0]
            if rule.startswith("!"):
                exceptions.add(to_ascii(rule[1:]))
            elif rule.startswith("*."):
                wildcards.add(to_ascii(rule[2:]))
            else:
                rules.add(to_ascii(rule))
        if not rules:
            raise ValueError("no public suffix rules found")
        return cls(frozenset(rules), frozenset(wildcards), frozenset(exceptions))

    def suffix_size(self, labels: Sequence[str]) -> int:
        """How many trailing labels make the public suffix of the name with these `labels`
        (lowercase ASCII, as `to_ascii(host).split(".")` gives; none of them empty): the PSL
        algorithm.

        An exception rule wins, and its suffix is the rule without its leftmost label
        (`!city.kawasaki.jp` → `kawasaki.jp`); otherwise the longest matching rule, a wildcard
        matching any one label in place of its `*`; otherwise the TLD alone (the default `*`).
        """
        names = [".".join(labels[-size:]) for size in range(len(labels), 0, -1)]
        for name in names:
            if name in self.exceptions:
                return name.count(".")
        for name in names:
            parent = name.partition(".")[2]
            if name in self.rules or (parent and parent in self.wildcards):
                return name.count(".") + 1
        return 1


@cache
def bundled() -> PublicSuffixList:
    """The list shipped with the package, parsed once."""
    text = files("inbox_jev").joinpath(DATA_FILE).read_text(encoding="utf-8")
    return PublicSuffixList.parse(text)


def registrable_domain(host: str) -> str | None:
    """`host`'s registrable domain in lowercase ASCII (`linkedin.co.uk` for
    `bounce.linkedin.co.uk`, `reply.wa.gov.au` for `kim.reply.wa.gov.au`), or None when `host`
    is itself a public suffix (`co.uk`, `github.io`) and so belongs to no one, or has an empty
    label (`""`, `.com`, `.example.com`, `a..com`), as the list's test vectors require: such a
    name isn't a domain anyone owns.
    """
    labels = to_ascii(host).split(".")
    if "" in labels:
        return None
    keep = bundled().suffix_size(labels) + 1
    return ".".join(labels[-keep:]) if len(labels) >= keep else None


def is_public_suffix(domain: str) -> bool:
    """Whether `domain` is itself a public suffix (`co.uk`, `wa.gov.au`, `com.ai`, `github.io`,
    `xn--55qx5d.xn--j6w193g`), not a name anyone owns; so is a name with an empty label. A name
    under no rule but a TLD is a domain (`acme.io`, `sap.de`).
    """
    return registrable_domain(domain) is None
