"""Read-only check of classification changes against real mail.

For each email: the category stored in the DB (if any), then what this checkout's code and
categories.yaml decide now (category, confidence, outcome, flags, top-3 probabilities), with
From and Subject truncated, and a final count of Jev calls. Never prints bodies.

`--reference` checks the account's reference set instead (`select_reference`): emails whose
category the user corrected, the correction being the truth, and with `--accepted DAYS` (opt-in,
weaker) emails left untouched for DAYS days after a label/act pass, the stored decision being
the truth. It always asks Jev (a correction is also a sender rule to the right answer) and ends
with `ReferenceSummary`: the branch's accuracy against the truth, the stored decision's (what
Jev decided then, on the deployed code) as the baseline, the mismatches by category, how many of
Jev's answers fall near their category's threshold and the Jev call count. Corrections whose
stored decision wasn't Jev's are scored for the branch but counted as `baseline_unknown`.
`--quiet` prints only the summary (category names and counts: no ids, From or Subject), to paste
into a PR.

It never writes Gmail or the DB: Gmail is only read (`Gmail.list_ids`/`get`/`labels`, the
sent-mail search), `gmail.modify`/`send` are never called, a SQLite DB is opened with mode=ro and
nothing is committed. Calls Jev (TYPESAFE_API_KEY) as `run` would: once per email, except for
rules to archive or spam categories (allowlisted and keep-rule emails are asked for the flags
only), and runs the sent-mail search only where `run` does (in the account's mode).
`scripts/check_branch.sh` runs it on the server, in the app container (docs/development.md,
"Checking a branch against real mail").
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import Engine, create_engine, make_url, select
from sqlalchemy.orm import Session

from inbox_jev import corrections, jev_guard, rules, triage
from inbox_jev.classification import Classification, PreviousContact
from inbox_jev.classifier import Classifier, JevError, JevState, describe_jev_error
from inbox_jev.config import (
    Account,
    CategoriesConfig,
    ConfigError,
    Mode,
    Settings,
    find_account,
    load_accounts,
    load_categories,
    validate_accounts,
)
from inbox_jev.db import Message, MessageStatus, now
from inbox_jev.gmail import Email, connect, is_not_found
from inbox_jev.policy import Decision, DecisionSource, category_threshold, decide

# The "borderline band": Jev's answers this close to their category's threshold, either side.
BANDS = (0.05, 0.10)


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _read_only_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        path = Path(parsed.database).resolve()
        if not path.exists():
            sys.exit(f"DB not found: {path}")
        url = f"sqlite:///file:{path}?mode=ro&uri=true"
    return create_engine(url)  # no create_all / migrations: nothing is written


def _outcome(decision: Decision) -> str:
    if decision.spam:
        return "SPAM"
    if decision.archive:
        return "ARCHIVE"
    return "REVIEW" if decision.low_confidence else "KEEP"


class TruthKind(StrEnum):
    """Where a reference email's truth comes from (see `select_reference`)."""

    CORRECTED = "corrected"  # the user corrected the category: `corrected_category`
    ACCEPTED = "accepted"  # opt-in, weaker: left untouched, so the stored `category`


@dataclass(frozen=True)
class ReferenceEmail:
    row: Message
    kind: TruthKind

    @property
    def truth(self) -> str:
        truth = self.row.corrected_category if self.kind is TruthKind.CORRECTED else None
        return truth or self.row.category or ""

    @property
    def stored(self) -> str | None:
        """The baseline: the category Jev decided on the deployed code, or None when unknown.

        A correction's stored `category` is only Jev's original answer when `source` is `jev`.
        A rule's or the allowlist's answer isn't like-for-like with a branch that ignores rules,
        and a redo after the correction (an `act` pass over a `label` row, `reprocess`) stores
        the correction rule's answer over the original, with `source` `rule`.
        """
        if self.kind is TruthKind.CORRECTED and self.row.source != DecisionSource.JEV:
            return None
        return self.row.category or ""


@dataclass(frozen=True)
class ReferenceSelection:
    emails: list[ReferenceEmail]
    unknown_category: int = 0  # truth or stored category no longer in categories.yaml: left out


def select_reference(
    session: Session,
    cfg: CategoriesConfig,
    account: str,
    *,
    limit: int,
    since: timedelta | None = None,
    accepted_after: timedelta | None = None,
) -> ReferenceSelection:
    """The account's reference set: at most `limit` emails of each kind, newest first. Read-only.

    Both kinds are `done` rows with a stored category, processed in the last `since` if given.

    - `corrected`: rows with a `corrected_category` (`corrections.detect`: a different category
      label, the Review label removed from a suggestion, or an email we archived or spammed
      pulled back to the inbox, which counts as `important`). Restored rows count: the
      correction was the user's. The truth is the corrected category; the baseline is the
      stored `category`, what Jev decided before the correction, only when `source` is `jev`.
      Otherwise it is unknown (`ReferenceEmail.stored` is None): a rule or the allowlist
      decided, or a later pass redid the email (e.g. after a mode change) and stored the
      correction rule's answer over Jev's.
    - `accepted`, only with `accepted_after`: rows from a `label`/`act` pass at least that long
      ago, not low confidence (a suggestion left in Review isn't accepted), not restored, with
      no correction. The truth is the stored category, so the baseline is right by definition.
      `Checker.run_reference` also drops the ones whose current Gmail labels show an edit.

    Rows whose truth or known baseline category isn't in `cfg` are left out and counted.
    """
    query = select(Message).where(
        Message.account == account,
        Message.status == MessageStatus.DONE,
        Message.category.is_not(None),
    )
    if since is not None:
        query = query.where(Message.processed_at >= now() - since)
    newest = Message.processed_at.desc()
    picked = [
        ReferenceEmail(row, TruthKind.CORRECTED)
        for row in session.scalars(
            query.where(Message.corrected_category.is_not(None)).order_by(newest).limit(limit)
        )
    ]
    if accepted_after is not None:
        accepted = query.where(
            Message.corrected_category.is_(None),
            Message.mode != Mode.DRY_RUN,
            Message.low_confidence.is_(False),
            Message.restored.is_(False),
            Message.processed_at <= now() - accepted_after,
        )
        picked += [
            ReferenceEmail(row, TruthKind.ACCEPTED)
            for row in session.scalars(accepted.order_by(newest).limit(limit))
        ]
    known = [
        r for r in picked if r.truth in cfg.by_key and (r.stored is None or r.stored in cfg.by_key)
    ]
    return ReferenceSelection(known, unknown_category=len(picked) - len(known))


def edited_since(cfg: CategoriesConfig, row: Message, current: set[str]) -> bool:
    """Whether the email's current labels (names) show the user edited it after its pass: a
    correction `corrections.detect` would learn, or an edit `triage.user_edit` hands off."""
    return (
        corrections.detect(row, current, cfg) is not None
        or triage.user_edit(cfg, row, current) is not None
    )


@dataclass(frozen=True)
class Outcome:
    """One reference email checked: its truth, the stored decision (None: baseline unknown) and
    the branch's."""

    kind: TruthKind
    truth: str
    stored: str | None
    branch: str
    # |confidence - threshold| of Jev's category, when Jev decided; None otherwise.
    margin: float | None = None


def _verdict(answer: str | None, truth: str) -> str:
    if answer is None:
        return "unknown"
    return "ok" if answer == truth else "WRONG"


def _ratio(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total:.1%})" if total else "0/0 (n/a)"


@dataclass
class ReferenceSummary:
    """What `--reference` prints at the end: counts and category names only."""

    outcomes: list[Outcome]
    jev_calls: int
    failed: int = 0  # Jev failed: no branch answer
    gone: int = 0  # deleted from Gmail
    edited: int = 0  # accepted rows the user edited since: not accepted after all
    unknown_category: int = 0

    def of(self, kind: TruthKind) -> list[Outcome]:
        return [o for o in self.outcomes if o.kind is kind]

    def within(self, band: float) -> int:
        # Rounded: 0.75 - 0.80 is 0.05000000000000004 in floats.
        return sum(1 for o in self.outcomes if o.margin is not None and round(o.margin, 6) <= band)

    def lines(self) -> list[str]:
        corrected, accepted = self.of(TruthKind.CORRECTED), self.of(TruthKind.ACCEPTED)
        out = [
            f"reference: emails={len(self.outcomes)} corrected={len(corrected)}"
            f" accepted={len(accepted)} jev_calls={self.jev_calls} failed={self.failed}"
            f" gone={self.gone} edited={self.edited} unknown_category={self.unknown_category}",
        ]
        for kind, rows in ((TruthKind.CORRECTED, corrected), (TruthKind.ACCEPTED, accepted)):
            if not rows:
                continue
            line = f"{kind}: branch={_ratio(sum(o.branch == o.truth for o in rows), len(rows))}"
            if kind is TruthKind.CORRECTED:
                # The baseline is scored on the rows Jev decided only (`ReferenceEmail.stored`).
                known = [
                    (o.branch == o.truth, o.stored == o.truth) for o in rows if o.stored is not None
                ]
                fixed = sum(b and not s for b, s in known)
                broken = sum(s and not b for b, s in known)
                line += (
                    f" stored={_ratio(sum(s for _, s in known), len(known))}"
                    f" fixed={fixed} broken={broken} baseline_unknown={len(rows) - len(known)}"
                )
            else:
                line += " (the stored decision is the truth)"
            out.append(line)
            out += [f"  {kind} {pair}" for pair in _confusion(rows)]
        jev = sum(1 for o in self.outcomes if o.margin is not None)
        bands = " ".join(f"within_{band:.2f}={self.within(band)}" for band in BANDS)
        out.append(f"borderline (Jev's category vs its threshold): {bands} of jev_decided={jev}")
        return out


def _confusion(rows: Iterable[Outcome]) -> list[str]:
    """`truth -> answer: branch=N stored=M` for each wrong pair, most frequent first."""
    branch = Counter((o.truth, o.branch) for o in rows if o.branch != o.truth)
    stored = Counter((o.truth, o.stored) for o in rows if o.stored not in (None, o.truth))
    pairs = sorted(branch.keys() | stored.keys(), key=lambda p: (-branch[p] - stored[p], p))
    return [f"{t} -> {a}: branch={branch[t, a]} stored={stored[t, a]}" for t, a in pairs]


@dataclass
class _CountingClassifier:
    """`classifier` counting the calls made to it, failed ones included."""

    classifier: jev_guard.Classifies
    calls: int = 0

    def classify(self, state: JevState) -> Classification:
        self.calls += 1
        return self.classifier.classify(state)


@dataclass(frozen=True)
class _Checked:
    """One email decided by this checkout (`Checker.decide`)."""

    decision: Decision
    classification: Classification | None
    contact: PreviousContact | None
    stale_rule: bool
    flags_failed: bool


@dataclass
class Checker:
    """Decides emails as this checkout's code would, read-only, and prints one report each."""

    session: Session
    gmail: triage.GmailApi
    classifier: jev_guard.Classifies
    cfg: CategoriesConfig
    account: Account
    mode: Mode  # the sent-mail search on rules depends on it
    body_chars: int
    ignore_rules: bool = False
    _jev: _CountingClassifier = field(init=False, repr=False)
    _sources: triage.InputSources = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._jev = _CountingClassifier(self.classifier)
        self._sources = triage.InputSources(
            self.cfg, self.session, self.gmail, self._jev, self.account, self.mode, self.body_chars
        )

    @property
    def jev_calls(self) -> int:
        return self._jev.calls

    def run(self, ids: list[str], *, quiet: bool = False) -> None:
        """Print each email's report (unless `quiet`), then the totals.

        A failed Jev call prints only `describe_jev_error` (type, status, request id: never the
        SDK's text, which can echo the email) and the next email goes on.
        """
        for message_id in ids:
            try:
                report = self.report(message_id)
            except JevError as exc:
                report = f"{message_id}  JEV FAILED: {describe_jev_error(exc)}"
            if not quiet:
                print(report)
        print(f"emails={len(ids)} jev_calls={self.jev_calls}")

    def run_reference(self, selection: ReferenceSelection, *, quiet: bool = False) -> None:
        """Decide each reference email, print its report and truth (unless `quiet`), then the
        `ReferenceSummary`. Emails deleted from Gmail, accepted ones the user edited since
        (`edited_since`) and failed Jev calls are counted, not scored. Rules are the caller's
        to ignore (`ignore_rules`): a correction is also a rule to the truth.
        """
        label_ids = self.gmail.labels()  # name -> id, to read each email's current labels
        summary = ReferenceSummary([], 0, unknown_category=selection.unknown_category)
        for ref in selection.emails:
            message_id = ref.row.gmail_id
            try:
                email = self.gmail.get(message_id)
            except Exception as exc:
                if not is_not_found(exc):
                    raise
                summary.gone += 1
                continue
            if ref.kind is TruthKind.ACCEPTED and edited_since(
                self.cfg, ref.row, triage.current_label_names(email, label_ids)
            ):
                summary.edited += 1
                continue
            try:
                checked = self.decide(email)
            except JevError as exc:
                summary.failed += 1
                if not quiet:
                    print(f"{message_id}  JEV FAILED: {describe_jev_error(exc)}")
                continue
            outcome = Outcome(
                ref.kind, ref.truth, ref.stored, checked.decision.category, self._margin(checked)
            )
            summary.outcomes.append(outcome)
            if not quiet:
                print(self._format(email, ref.row, checked))
                print(
                    f"    truth={outcome.truth} ({outcome.kind})"
                    f"  branch={_verdict(outcome.branch, outcome.truth)}"
                    f"  stored={_verdict(outcome.stored, outcome.truth)}"
                )
        summary.jev_calls = self.jev_calls
        print("\n".join(summary.lines()))

    def _margin(self, checked: _Checked) -> float | None:
        """How far Jev's top answer is from its category's threshold, when Jev decided."""
        answer = checked.classification
        if checked.decision.source != DecisionSource.JEV or answer is None:
            return None
        if answer.category not in self.cfg.by_key:
            return None
        return abs(answer.confidence - category_threshold(self.cfg, self.account, answer.category))

    def report(self, message_id: str) -> str:
        """One email's lines: stored and new decision, flags, top-3, From and Subject truncated.

        Raises `JevError` when Jev decides the email and the call fails.
        """
        email = self.gmail.get(message_id)
        old = self.session.scalar(
            select(Message).where(
                Message.account == self.account.email, Message.gmail_id == message_id
            )
        )
        return self._format(email, old, self.decide(email))

    def decide(self, email: Email) -> _Checked:
        """Decide `email` as `run` would now. Raises `JevError` when Jev's call fails."""
        cfg, account = self.cfg, self.account
        sig = triage.signals_for(email, account)
        rule, stale = (
            (None, None)
            if self.ignore_rules
            else rules.load(self.session, account.email, sig.sender)
            .deciding(account)
            .match(cfg.by_key)
        )
        calls_before = self._jev.calls
        # The same calls as `run`: Jev with the sent-mail search when no rule or allowlist
        # decides, Jev for the flags only on keep rules and the allowlist, and the sent-mail
        # search on a rule only when it could unsubscribe.
        manual_rule = rules.is_manual(rule)
        inputs = triage.gather_inputs(
            self._sources, email, sig, rule.category if rule else None, manual_rule=manual_rule
        )
        classification, contact = inputs.classification, inputs.contact
        decision = decide(
            cfg,
            account,
            sig,
            classification,
            inputs.rule_category,
            previous_contact=contact,
            manual_rule=manual_rule,
        )
        return _Checked(
            decision,
            classification,
            contact,
            stale_rule=bool(stale),
            # Only the flags-only call's failure leaves no answer: Jev's own failure raises.
            flags_failed=classification is None and self._jev.calls > calls_before,
        )

    def _format(self, email: Email, old: Message | None, checked: _Checked) -> str:
        cfg, decision, classification = self.cfg, checked.decision, checked.classification
        sig = triage.signals_for(email, self.account)
        flags = [
            f"src={decision.source}",
            f"contact={checked.contact}",
            *(["no_reply"] if sig.no_reply else []),
            *(["group"] if decision.archive_group else []),
            *(["unsub"] if decision.unsubscribe else []),
            *(["unverified"] if decision.unsubscribe_skipped_unverified else []),
            *(["known_contact"] if decision.known_contact_kept else []),
            *(["needs_reply"] if cfg.needs_reply.label in decision.add_labels else []),
            *(["stale_rule"] if checked.stale_rule else []),
            *(["flags_failed"] if checked.flags_failed else []),
        ]
        acts = self.account.act_categories
        if acts is not None and decision.category not in acts:
            flags.append("not_in_act_categories")
        top = ""
        if classification:
            best = sorted(classification.probabilities.items(), key=lambda kv: -kv[1])
            top = " ".join(f"{k}={v:.2f}" for k, v in best[:3])
        was = f"{old.category}@{old.confidence or 0:.2f}" if old else "-"
        return (
            f"{email.id}  was={was}  now={decision.category}@{decision.confidence:.2f}"
            f"  {_outcome(decision)} [{' '.join(flags)}]  {top}\n"
            f"    from={_short(email.header('From'), 60)!r}"
            f"  subject={_short(email.header('Subject'), 70)!r}"
        )


def _days(value: str) -> timedelta:
    """An argparse type: a whole number of days, at least 1."""
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of days: {value!r}") from None
    if days < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 day, got {days}")
    return timedelta(days=days)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--account", required=True)
    p.add_argument("--query", help="Gmail search, e.g. 'in:inbox newer_than:7d'")
    p.add_argument("--ids", nargs="*", default=[], help="Gmail message ids")
    p.add_argument(
        "--limit", type=int, default=30, help="Max emails from --query, or of each --reference kind"
    )
    p.add_argument("--categories", type=Path, help="categories.yaml (default: CATEGORIES_FILE)")
    p.add_argument("--ignore-rules", action="store_true", help="Ask Jev even if a rule matches")
    p.add_argument(
        "--reference",
        action="store_true",
        help="Check the emails the user corrected (and --accepted ones) and print the summary",
    )
    p.add_argument(
        "--since", type=_days, metavar="DAYS", help="--reference: only rows processed since"
    )
    p.add_argument(
        "--accepted",
        type=_days,
        metavar="DAYS",
        help="--reference: also emails left untouched DAYS days after a label/act pass (weaker)",
    )
    p.add_argument(
        "--quiet", action="store_true", help="Print only the totals or summary: no From/Subject"
    )
    args = p.parse_args()
    if args.reference:
        if args.query or args.ids:
            p.error("--reference checks the stored reference set: no --query or --ids")
    elif not args.query and not args.ids:
        p.error("give --query and/or --ids, or --reference")
    elif args.since or args.accepted:
        p.error("--since and --accepted go with --reference")
    return args


def main() -> None:
    args = _parse_args()
    s = Settings()
    cfg = load_categories(args.categories or s.categories_file)
    all_accounts = load_accounts(s.accounts_file)
    try:
        validate_accounts(all_accounts, cfg)  # as every CLI command does: the whole file
    except ConfigError as exc:
        sys.exit(f"{s.accounts_file}: {exc}")
    account = find_account(all_accounts, args.account)
    if account is None:
        sys.exit(f"{args.account} is not in {s.accounts_file}")

    with connect(s.google_tokens_dir, account.email) as gmail:
        engine = _read_only_engine(s.database_url)
        try:
            with (
                closing(Classifier(s.typesafe_api_key, s.typesafe_model, cfg)) as classifier,
                Session(engine) as session,
            ):
                checker = Checker(
                    session,
                    gmail,
                    classifier,
                    cfg,
                    account,
                    triage.effective_mode(s, account, None),
                    s.body_chars,
                    # A correction is also a sender rule to the truth: always ask Jev.
                    ignore_rules=args.ignore_rules or args.reference,
                )
                if args.reference:
                    selection = select_reference(
                        session,
                        cfg,
                        account.email,
                        limit=args.limit,
                        since=args.since,
                        accepted_after=args.accepted,
                    )
                    checker.run_reference(selection, quiet=args.quiet)
                else:
                    ids = list(args.ids)
                    if args.query:
                        ids += [i for i in gmail.list_ids(args.query, args.limit) if i not in ids]
                    checker.run(ids, quiet=args.quiet)
                session.rollback()
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
