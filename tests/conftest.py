from pathlib import Path

import pytest

from inbox_jev.classifier import Classification
from inbox_jev.config import Account, load_categories
from inbox_jev.signals import Signals

ROOT = Path(__file__).resolve().parents[1]

GMAIL_PASS = (
    "mx.google.com; dkim=pass header.i=@news.shop.com; spf=pass; dmarc=pass header.from=shop.com"
)


@pytest.fixture
def cfg():
    return load_categories(ROOT / "categories.yaml")


@pytest.fixture
def account():
    return Account(
        email="me@acme.com", allow_domains=["acme.com"], allow_senders=["vip@partner.com"]
    )


def make_signals(**kw) -> Signals:
    base = dict(
        sender="promo@news.shop.com",
        sender_domain="news.shop.com",
        list_id="news.shop.com",
        unsubscribe_https=["https://shop.com/u/123"],
        unsubscribe_mailto=["mailto:unsub@shop.com?subject=unsubscribe"],
        one_click=True,
        dmarc_pass=True,
    )
    base.update(kw)
    return Signals(**base)


def make_classification(category="marketing", confidence=0.97, needs_reply=0.1, tags=None):
    return Classification(
        category=category,
        confidence=confidence,
        probabilities={category: confidence},
        needs_reply=needs_reply,
        tags=tags or {},
    )
