"""Read-only check of classification changes against real mail.

For each email: the category stored in the DB (if any), then what this checkout's code and
categories.yaml decide now (category, confidence, outcome, flags, top-3 probabilities), with
From and Subject truncated, and a final count of Jev calls. Never prints bodies.

It never writes Gmail or the DB: Gmail is only read (`Gmail.list_ids`/`get`, the sent-mail search),
`gmail.modify`/`send` are never called, a SQLite DB is opened with mode=ro and nothing is
committed. Calls Jev (TYPESAFE_API_KEY) as `run` would: once per email, except for rules to
archive or spam categories (allowlisted and keep-rule emails are asked for the flags only), and
runs the sent-mail search only where `run` does (in the account's mode).
See CLAUDE.md "Validating behaviour changes" for the docker compose command.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, create_engine, make_url, select
from sqlalchemy.orm import Session

from inbox_jev import jev_guard, rules, triage
from inbox_jev.classification import Classification
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
from inbox_jev.db import Message
from inbox_jev.gmail import connect
from inbox_jev.policy import Decision, decide


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


@dataclass
class _CountingClassifier:
    """`classifier` counting the calls made to it, failed ones included."""

    classifier: jev_guard.Classifies
    calls: int = 0

    def classify(self, state: JevState) -> Classification:
        self.calls += 1
        return self.classifier.classify(state)


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

    def run(self, ids: list[str]) -> None:
        """Print each email's report, then the totals.

        A failed Jev call prints only `describe_jev_error` (type, status, request id: never the
        SDK's text, which can echo the email) and the next email goes on.
        """
        for message_id in ids:
            try:
                report = self.report(message_id)
            except JevError as exc:
                report = f"{message_id}  JEV FAILED: {describe_jev_error(exc)}"
            print(report)
        print(f"emails={len(ids)} jev_calls={self.jev_calls}")

    def report(self, message_id: str) -> str:
        """One email's lines: stored and new decision, flags, top-3, From and Subject truncated.

        Raises `JevError` when Jev decides the email and the call fails.
        """
        cfg, account, session = self.cfg, self.account, self.session
        email = self.gmail.get(message_id)
        sig = triage.signals_for(email, account)
        old = session.scalar(
            select(Message).where(Message.account == account.email, Message.gmail_id == message_id)
        )
        rule, stale = (
            (None, None)
            if self.ignore_rules
            else rules.match(session, account.email, sig.sender, cfg.by_key)
        )
        calls_before = self._jev.calls
        # The same calls as `run`: Jev with the sent-mail search when no rule or allowlist
        # decides, Jev for the flags only on keep rules and the allowlist, and the sent-mail
        # search on a rule only when it could unsubscribe.
        inputs = triage.gather_inputs(self._sources, email, sig, rule)
        classification, contact = inputs.classification, inputs.contact
        # Only the flags-only call's failure leaves no answer: Jev's own failure raises.
        flags_failed = classification is None and self._jev.calls > calls_before
        decision = decide(
            cfg,
            account,
            sig,
            classification,
            inputs.rule_category,
            previous_contact=contact,
        )
        flags = [
            f"src={decision.source}",
            f"contact={contact}",
            *(["no_reply"] if sig.no_reply else []),
            *(["group"] if decision.archive_group else []),
            *(["unsub"] if decision.unsubscribe else []),
            *(["unverified"] if decision.unsubscribe_skipped_unverified else []),
            *(["known_contact"] if decision.known_contact_kept else []),
            *(["needs_reply"] if cfg.needs_reply.label in decision.add_labels else []),
            *(["stale_rule"] if stale else []),
            *(["flags_failed"] if flags_failed else []),
        ]
        acts = account.act_categories
        if acts is not None and decision.category not in acts:
            flags.append("not_in_act_categories")
        top = ""
        if classification:
            best = sorted(classification.probabilities.items(), key=lambda kv: -kv[1])
            top = " ".join(f"{k}={v:.2f}" for k, v in best[:3])
        was = f"{old.category}@{old.confidence or 0:.2f}" if old else "-"
        return (
            f"{message_id}  was={was}  now={decision.category}@{decision.confidence:.2f}"
            f"  {_outcome(decision)} [{' '.join(flags)}]  {top}\n"
            f"    from={_short(email.header('From'), 60)!r}"
            f"  subject={_short(email.header('Subject'), 70)!r}"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--account", required=True)
    p.add_argument("--query", help="Gmail search, e.g. 'in:inbox newer_than:7d'")
    p.add_argument("--ids", nargs="*", default=[], help="Gmail message ids")
    p.add_argument("--limit", type=int, default=30, help="Max emails from --query")
    p.add_argument("--categories", type=Path, help="categories.yaml (default: CATEGORIES_FILE)")
    p.add_argument("--ignore-rules", action="store_true", help="Ask Jev even if a rule matches")
    args = p.parse_args()
    if not args.query and not args.ids:
        p.error("give --query and/or --ids")

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
        ids = list(args.ids)
        if args.query:
            ids += [i for i in gmail.list_ids(args.query, args.limit) if i not in ids]

        engine = _read_only_engine(s.database_url)
        try:
            with (
                closing(Classifier(s.typesafe_api_key, s.typesafe_model, cfg)) as classifier,
                Session(engine) as session,
            ):
                Checker(
                    session,
                    gmail,
                    classifier,
                    cfg,
                    account,
                    triage.effective_mode(s, account, None),
                    s.body_chars,
                    args.ignore_rules,
                ).run(ids)
                session.rollback()
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
