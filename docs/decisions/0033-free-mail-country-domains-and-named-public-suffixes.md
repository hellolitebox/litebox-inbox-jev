# 0033. Free mail includes country domains; `group_domains` refuses public suffixes by name

- Status: Accepted (public suffixes by name superseded by [0037](0037-public-suffix-list-for-suffixes-and-registrable-domains.md))
- Date: 2026-10-13
- PR: #35

## Context
`config.FREE_MAIL` listed 13 providers by their main domain only. Free mail is skipped by the
`previous_contact` domain search: anyone can get an address there, so writing to one person on
it says nothing about the others. `yahoo.com.ar`, `hotmail.co.uk`, `outlook.com.br` and the like
weren't listed, so after writing to one friend at `hotmail.com.ar`, every `hotmail.com.ar`
sender was `wrote_to_their_domain`: a known contact, never unsubscribed, any reply signal
blocking the archive group, and no auto rule ([0011](0011-never-unsubscribe-groups-or-known-contacts.md),
[0008](0008-needs-reply-veto-with-keep-signal.md)).

`group_domains` ([0012](0012-group-author-trust-opt-in.md)) refused a bare public suffix only
when its second-level label was in a short list (`co`, `com`, `org`…), so `gouv.fr`, `nhs.uk`,
`adv.br` and `govt.nz` were accepted as trusted domains: every Google Group under them would be
trusted to name its author. `signals` (ADR 0032) has its own suffix rule for registrable domains:
any second-level label of up to 4 characters under a two-letter TLD.

## Decision
- `FREE_MAIL` lists the big consumer providers with their common country domains (Microsoft's
  `hotmail`/`outlook`/`live`, Yahoo's, `googlemail.com`), other well-known consumer providers
  (`mac.com`, `aim.com`, `pm.me`, Tuta, Fastmail, GMX/Web.de, Mail.com, Yandex, Mail.ru, QQ,
  163/126, Naver) and the largest regional webmail and ISP mail (UOL, BOL, Terra, Fibertel,
  Arnet, Libero, Orange, Wanadoo, Free, La Poste, T-Online, BT). It also covers `group_domains`,
  which already refuses free mail.
- The ccTLD second-level suffixes live in one place, `signals.PUBLIC_SLDS`, extended with
  `gouv`, `govt`, `gub`, `nhs`, `adv`, `asso`, `mil`, `nom`, `ltd`, `plc`, `sch`.
  `signals.is_public_suffix` (two labels, two-letter TLD, second level in that set) replaces the
  list in `config`, and `signals.DOMAIN` replaces its domain pattern (a TLD of 2+ letters or a
  punycode IDN TLD, `xn--p1ai`, as the old pattern accepted). A test pins every listed name
  within `_registrable_size`'s length rule, so the two rules never disagree on a listed suffix.
- Under ccTLDs sold as generic names, `PUBLIC_SLDS` doesn't apply: `signals.GENERIC_CCTLD_SLDS`
  gives each one exactly the second-level names the Public Suffix List (ICANN section) has under
  it, and only those are refused: `.ai` (`com`, `net`, `off`, `org`), `.co` (`com`, `edu`, `gov`,
  `mil`, `net`, `nom`, `org`), `.io` (`co`, `com`, `edu`, `gov`, `mil`, `net`, `nom`, `org`),
  `.me` (`ac`, `co`, `edu`, `gov`, `its`, `net`, `org`, `priv`), `.tv` (none). Checked against
  PSL `VERSION: 2026-10-07_07-28-19_UTC`, `COMMIT: 3929462652695bad04f0a27afb600974014a3c8b`
  (the `.co` registry dropped `arts`, `firm`, `info`, `int`, `rec`, `web` on 2024-11-18).
  `tests/test_signals.py` pins that snapshot, and checks that no name the old `config` list
  refused and the PSL lists (`org.io`, `gov.co`) is accepted. Companies register short names
  there (`adv.ai`, `sch.ai`, `plc.io`, `web.co`), and refusing one fails the accounts file.
  `com.co` stays refused: it is a real Colombian suffix. These names are within the length rule
  too (pinned by the same test).

## Alternatives considered
- **Reuse `_registrable_size`'s length rule as is** for `group_domains`: it also takes short
  company names under a ccTLD as suffixes (`acme.io`, `sap.de`, `ing.nl`, `lume.ai`). That is the
  safe side for machine hosts (fewer machines), but here it would refuse real domains, and a
  refused entry fails the whole accounts file at load, stopping every account's runs after a
  deploy. Naming the suffixes refuses only what is known to be one.
- **The public suffix list** (`publicsuffixlist`, `tldextract`): would also catch deeper
  suffixes (`wa.gov.au`). Rejected for the same reason as in ADR 0032: a dependency, and a list
  that must be kept fresh, for an opt-in setting.
- **`PUBLIC_SLDS` under every two-letter TLD** (the first version of this PR): refuses
  `adv.ai`, `plc.io` or `sch.ai`, real company domains, under the TLDs most used as generic
  names; the generic-use exceptions are few and their PSL entries short.
- **Exempt generic-use ccTLDs entirely**: would accept `com.ai` and `com.co`, which are public
  suffixes.
- **Change `_registrable_size` to the same table**: would make `bounce.adv.ai` a machine host,
  which changes `previous_contact` on real mail (more machines, fewer known contacts, the unsafe
  side) for no reported case; the length rule's error stays on the safe side.
- **Match free mail by brand on any TLD** (`hotmail.*`, `yahoo.*`): someone could register
  `yahoo.<new TLD>` as a company domain; an explicit list says exactly what is skipped.

## Consequences
- A sender on a listed country free-mail domain gets only the address search; `wrote_to_sender`
  still makes them a known contact (EX-26).
- `gouv.fr`, `nhs.uk`, `adv.br`, `govt.nz`, `asso.fr`, `com.ai`, `com.co`… in `group_domains`
  now fail the accounts file at load (EX-27), while `adv.ai` or `plc.io` load. The production
  config (`litebox.ai`) is unaffected.
- Another ccTLD sold as a generic name (`.ly`, `.gg`, `.fm`…) still gets `PUBLIC_SLDS`, so a
  company at e.g. `sch.gg` is refused until that TLD is added to `GENERIC_CCTLD_SLDS` with its
  PSL names (Known gaps). `.ai`, `.co`, `.io`, `.me` and `.tv` are the ones used most for company
  domains; the table is the place to add more.
- Deeper suffixes (`wa.gov.au`) are still accepted, and a free-mail provider missing from the
  list is still searched as a company domain: both in the spec's Known gaps.
