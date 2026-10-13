"""Read-only check of classification changes against real mail.

For each email: the category stored in the DB (if any), then what this checkout's code and
categories.yaml decide now (category, confidence, outcome, flags, top-3 probabilities), with
From and Subject truncated. Never prints bodies.

It never writes Gmail or the DB: Gmail is only read (messages.list/get, the sent-mail search),
`gmail.modify`/`send` are never called, a SQLite DB is opened with mode=ro and nothing is
committed. Calls Jev (TYPESAFE_API_KEY), so each email costs one classification.
See CLAUDE.md "Validating behaviour changes" for the docker compose command.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from googleapiclient.discovery import build
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.orm import Session

from inbox_jev import rules, triage
from inbox_jev.classifier import Classifier, build_state
from inbox_jev.config import Settings, load_accounts, load_categories
from inbox_jev.db import Message
from inbox_jev.gmail import Gmail, load_credentials
from inbox_jev.policy import decide, is_allowed


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _read_only_engine(url: str):
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        path = Path(parsed.database).resolve()
        if not path.exists():
            sys.exit(f"DB not found: {path}")
        url = f"sqlite:///file:{path}?mode=ro&uri=true"
    return create_engine(url)  # no create_all / migrations: nothing is written


def _outcome(decision) -> str:
    if decision.spam:
        return "SPAM"
    if decision.archive:
        return "ARCHIVE"
    return "REVIEW" if decision.low_confidence else "KEEP"


def _ids(svc, query: str, limit: int) -> list[str]:
    ids: list[str] = []
    token = None
    while len(ids) < limit:
        resp = (
            svc.messages()
            .list(userId="me", q=query, maxResults=min(500, limit - len(ids)), pageToken=token)
            .execute()
        )
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    accounts = [
        a for a in load_accounts(s.accounts_file) if a.email.lower() == args.account.lower()
    ]
    if not accounts:
        sys.exit(f"{args.account} is not in {s.accounts_file}")
    account = accounts[0]

    creds = load_credentials(s.google_tokens_dir, account.email)
    gmail = Gmail(creds)
    ids = list(args.ids)
    if args.query:
        svc = build("gmail", "v1", credentials=creds, cache_discovery=False).users()
        ids += [i for i in _ids(svc, args.query, args.limit) if i not in ids]

    classifier = Classifier(s.typesafe_api_key, s.typesafe_model, cfg)
    try:
        with Session(_read_only_engine(s.database_url)) as session:
            for message_id in ids:
                email = gmail.get(message_id)
                sig = triage.signals_for(email, account)
                old = session.scalar(
                    select(Message).where(
                        Message.account == account.email, Message.gmail_id == message_id
                    )
                )
                rule = (
                    None if args.ignore_rules else rules.lookup(session, account.email, sig.sender)
                )
                classification, contact = None, None
                if not is_allowed(account, sig):
                    contact = triage.previous_contact(gmail, account.email, sig.sender)
                    if rule is None:
                        classification = classifier.classify(
                            build_state(email, account.email, s.body_chars, contact, sig)
                        )
                decision = decide(
                    cfg,
                    account,
                    sig,
                    classification,
                    rule.category if rule else None,
                    previous_contact=contact,
                )
                flags = [
                    f"src={decision.source}",
                    *(["group"] if decision.archive_group else []),
                    *(["unsub"] if decision.unsubscribe else []),
                    *(["unverified"] if decision.downgraded_to_spam else []),
                    *(["known_contact"] if decision.known_contact_kept else []),
                ]
                acts = account.act_categories
                if acts is not None and decision.category not in acts:
                    flags.append("not_in_act_categories")
                top = ""
                if classification:
                    best = sorted(classification.probabilities.items(), key=lambda kv: -kv[1])
                    top = " ".join(f"{k}={v:.2f}" for k, v in best[:3])
                was = f"{old.category}@{old.confidence or 0:.2f}" if old else "-"
                print(
                    f"{message_id}  was={was}  now={decision.category}@{decision.confidence:.2f}"
                    f"  {_outcome(decision)} [{' '.join(flags)}]  {top}\n"
                    f"    from={_short(email.header('From'), 60)!r}"
                    f"  subject={_short(email.header('Subject'), 70)!r}"
                )
            session.rollback()
    finally:
        classifier.close()


if __name__ == "__main__":
    main()
