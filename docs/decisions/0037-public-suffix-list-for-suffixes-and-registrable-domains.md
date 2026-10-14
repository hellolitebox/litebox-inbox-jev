# 0037. The Public Suffix List decides public suffixes and registrable domains

- Status: Accepted
- Date: 2026-10-13
- PR: #37

## Context
Two rules guessed what a public suffix is, without the Public Suffix List (PSL):

- `signals._registrable_size` ([0032](0032-machine-addresses-make-no-known-contact.md)) took the
  last three labels as the registrable domain under a two-letter TLD whose second label had at
  most 4 characters, else the last two. Under a deeper or longer suffix (`wa.gov.au`,
  `kommune.no`, `blogspot.com`) a company named `reply` or `bounce` (`kim@reply.wa.gov.au`) was
  a machine, so its sender was never searched for itself. Short names under a ccTLD (`sap.de`,
  `acme.io`) were taken as suffixes, so `bounce.sap.de` wasn't a machine host.
- `signals.is_public_suffix` ([0033](0033-free-mail-country-domains-and-named-public-suffixes.md))
  refused a `group_domains` entry only when it was two labels with a second level named in
  `PUBLIC_SLDS`, or in the hand-copied PSL names of five generic-use ccTLDs
  (`GENERIC_CCTLD_SLDS`). `wa.gov.au` was accepted, other generic-use ccTLDs (`.gg`, `.ly`) got
  the generic table (`sch.gg` refused, a real domain), and IDN suffixes in punycode were never
  refused.

ADRs 0032 and 0033 rejected the PSL as a new dependency plus a list to keep fresh. Both gaps are
in the spec's Known gaps; the list itself is ~330 KB of text with a simple, documented format.

## Decision
- **Vendor the list, not a library.** `src/inbox_jev/public_suffix_list.dat` is the PSL file as
  published (its header names its `VERSION` and `COMMIT`), shipped as package data (hatchling
  ships every file under `src/inbox_jev`; CI's `docker` job smoke-tests it in the image).
  `inbox_jev.psl` is a small pure parser of the PSL algorithm: normal rules, wildcards (`*.ck`),
  exceptions (`!www.ck`, which win), the default `*` (an unlisted TLD), and IDN: rules and input
  are compared as lowercase ASCII, each Unicode label as its punycode A-label, so `公司.香港` and
  `xn--55qx5d.xn--j6w193g` give the same answer. It is read once, lazily, and never fetched at
  runtime.
- **Both sections, for both uses.** The PSL has an ICANN section (registry suffixes: `co.uk`,
  `wa.gov.au`) and a PRIVATE one (platforms that hand out subdomains: `github.io`,
  `blogspot.com`).
  - `group_domains` (`config.normalize_group_domain` → `psl.is_public_suffix`): a PRIVATE suffix
    is as public as an ICANN one. Anyone can get `x.github.io`, so trusting `github.io` would
    trust anyone's group there; no company runs its Google Workspace on a platform suffix.
    Refusing more is the safe side; refusing a real domain only fails the accounts file at load.
  - Machine hosts (`signals.is_automated_address` → `psl.registrable_domain`): counting PRIVATE
    suffixes makes registrable domains longer (`reply.blogspot.com`, not `blogspot.com`), so
    fewer `bounce.` / `reply.` labels are left of it and fewer hosts are machines: the safe side
    of 0032 (a false machine hides a real contact). A service's real bounce host under a platform
    suffix stays a person, as before.
- `signals.PUBLIC_SLDS`, `GENERIC_CCTLD_SLDS`, `_PUBLIC_SLD_MAX_LEN`, `_registrable_size` and
  `signals.is_public_suffix` are deleted, with the snapshot tests that pinned them; the PSL tests
  cover every example they had.
- **Freshness.** `scripts/update_psl.py` downloads the list, checks it (exactly one `VERSION`
  and one `COMMIT` header line in the publisher's formats, both sections, at least 5,000 rules,
  known answers for each rule kind), writes it only when its `VERSION` is newer and prints its
  `VERSION` and `COMMIT`; `--check` does the same without ever writing. The weekly `Update
  public suffix list` workflow runs `--check` and, when a newer list exists, opens (or updates)
  an issue labeled `psl-update` with the validated values (`contents: read` and `issues: write`
  only). It never pushes or opens a PR, so the repo setting that lets Actions create pull
  requests stays off. A session ships the issue as a normal PR (the PSL lane in CLAUDE.md): the
  script on a branch, a check that every production `group_domains` entry still loads with the
  new list, and the normal QA loop.

## Alternatives considered
- **A library** (`publicsuffixlist`, `tldextract`): a new runtime dependency for ~100 lines of
  parser, and its bundled list is only as fresh as its releases (Dependabot would bump it).
  `tldextract` also fetches the list at runtime by default. `scripts/check_branch.sh` refuses
  branches that change `uv.lock`, so a dependency would also have needed a separate image to
  validate this change on real mail.
- **ICANN only for machine hosts**: would keep `reply.blogspot.com` a machine, the unsafe side,
  for no case where a PRIVATE suffix hosts a service's bounce addresses.
- **ICANN only for `group_domains`**: would accept `github.io` or `blogspot.com`, whose
  subdomains belong to anyone.
- **Fetch the list at runtime** (cached): a network call in every run, and a way for an outage
  or a bad download to change decisions. The vendored file changes only through a reviewed PR.
- **Keep the name tables and add more**: the gaps above are exactly what tables miss.

## Consequences
- `kim@reply.wa.gov.au`, `ola@reply.kommune.no`, `lee@reply.blogspot.com` are people; their
  address is searched (`wrote_to_sender`).
- `bounce.sap.de`, `bounce.adv.ai`, `reply.acme.io` are now machine hosts: a sent email only to
  such an address no longer makes `sap.de` a known contact (EX-32). Those are the real bounce
  subdomains the length rule missed.
- `group_domains` refuses `wa.gov.au`, `kommune.no`, `plc.ly`, `co.gg`, `github.io`,
  `blogspot.com` and IDN suffixes (`xn--55qx5d.xn--j6w193g`); it accepts `sch.gg`, `adv.ai`,
  `sap.de` and `nsw.gov.au` (removed from the PSL at New South Wales' request). Production's
  `litebox.ai` is unaffected (tested).
- A suffix added to the PSL after the vendored version is unknown until a session ships the weekly issue's update;
  a PSL update can make an existing `group_domains` entry a suffix, which fails the accounts file
  at load: the PSL lane checks production's entries before merging.
- [0032](0032-machine-addresses-make-no-known-contact.md)'s registrable-domain rule and
  [0033](0033-free-mail-country-domains-and-named-public-suffixes.md)'s suffixes by name are
  replaced by this record; the rest of both stands.
