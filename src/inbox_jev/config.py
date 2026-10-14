"""Runtime settings (.env) plus the accounts and categories YAML files."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .psl import is_public_suffix
from .signals import DOMAIN, domain_of, on_domain

# Gmail `newer_than:` units: days, months, years.
LOOKBACK_RE = re.compile(r"^\d+[dmy]$")

# Public mail providers: anyone can get an address there, so they say nothing about who a sender
# is (previous_contact) and can never be an account's trusted group domain. The big consumer
# providers with their country domains (writing to a friend at yahoo.com.ar must not make every
# yahoo.com.ar sender a known contact), and the largest regional ones. A provider missing here
# only costs a domain search that can make a stranger look known; adding one is always safe.
FREE_MAIL = frozenset(
    {
        # Google
        "gmail.com",
        "googlemail.com",
        # Microsoft
        "outlook.com",
        "outlook.com.ar",
        "outlook.com.br",
        "outlook.cl",
        "outlook.de",
        "outlook.es",
        "outlook.fr",
        "outlook.it",
        "hotmail.com",
        "hotmail.be",
        "hotmail.ca",
        "hotmail.co.uk",
        "hotmail.com.ar",
        "hotmail.com.br",
        "hotmail.com.mx",
        "hotmail.de",
        "hotmail.es",
        "hotmail.fr",
        "hotmail.it",
        "hotmail.nl",
        "live.com",
        "live.be",
        "live.ca",
        "live.cl",
        "live.co.uk",
        "live.com.ar",
        "live.com.mx",
        "live.de",
        "live.fr",
        "live.it",
        "live.nl",
        "msn.com",
        # Yahoo
        "yahoo.com",
        "yahoo.ca",
        "yahoo.co.in",
        "yahoo.co.jp",
        "yahoo.co.uk",
        "yahoo.com.ar",
        "yahoo.com.au",
        "yahoo.com.br",
        "yahoo.com.mx",
        "yahoo.de",
        "yahoo.es",
        "yahoo.fr",
        "yahoo.it",
        "ymail.com",
        "rocketmail.com",
        # Apple, AOL
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "aim.com",
        # Privacy-focused
        "proton.me",
        "protonmail.com",
        "protonmail.ch",
        "pm.me",
        "tutanota.com",
        "tuta.io",
        "fastmail.com",
        # GMX / 1&1, Mail.com
        "gmx.com",
        "gmx.net",
        "gmx.de",
        "gmx.at",
        "gmx.ch",
        "web.de",
        "mail.com",
        # Regional webmail and ISP mail
        "yandex.com",
        "yandex.ru",
        "mail.ru",
        "qq.com",
        "163.com",
        "126.com",
        "naver.com",
        "uol.com.br",
        "bol.com.br",
        "terra.com.br",
        "fibertel.com.ar",
        "arnet.com.ar",
        "libero.it",
        "orange.fr",
        "wanadoo.fr",
        "free.fr",
        "laposte.net",
        "t-online.de",
        "btinternet.com",
    }
)
# Public group hosts: anyone can create a group there.
PUBLIC_GROUP_HOSTS = frozenset({"googlegroups.com", "groups.google.com"})


def normalize_group_domain(value: str) -> str:
    """Normalised group_domains entry, or ValueError if it isn't a domain someone can own: not a
    domain name, a public suffix of the Public Suffix List (ICANN or PRIVATE section: `co.uk`,
    `wa.gov.au`, `github.io`), or on a public mail or group host."""
    domain = value.strip().lower().rstrip(".").removeprefix("@")
    if not DOMAIN.match(domain):
        raise ValueError(f"group_domains entry {value!r} isn't a domain like example.org")
    if is_public_suffix(domain):
        raise ValueError(f"group_domains entry {value!r} is a public suffix, not a domain")
    public = next((p for p in FREE_MAIL | PUBLIC_GROUP_HOSTS if on_domain(domain, p)), None)
    if public:
        raise ValueError(f"group_domains can't include public domains: {value!r} ({public})")
    return domain


def check_lookback(value: str) -> str:
    if not LOOKBACK_RE.match(value):
        raise ValueError(f"invalid lookback {value!r}: use a number plus d, m or y, e.g. 2d or 30d")
    return value


class Mode(StrEnum):
    DRY_RUN = "dry_run"
    LABEL = "label"
    ACT = "act"


class DigestLanguage(StrEnum):
    """Language of an account's Slack digest (the texts live in `digest.MESSAGES`)."""

    EN = "en"
    ES = "es"


class Action(StrEnum):
    KEEP = "keep"
    ARCHIVE = "archive"
    ARCHIVE_UNSUBSCRIBE = "archive_unsubscribe"
    SPAM = "spam"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    typesafe_api_key: str = ""
    typesafe_model: str = "jev-latest"
    slack_bot_token: str = ""
    database_url: str = "sqlite:///data/inbox-jev.db"
    google_client_secrets_file: Path = Path("secrets/client_secret.json")
    google_tokens_dir: Path = Path("secrets/tokens")
    accounts_file: Path = Path("accounts.yaml")
    categories_file: Path = Path("categories.yaml")
    mode: Mode = Mode.DRY_RUN
    lookback: str = "2d"
    max_messages_per_run: int = 200
    max_unsubscribes_per_run: int = 20
    body_chars: int = 3000
    log_level: str = "INFO"

    @field_validator("lookback")
    @classmethod
    def _check_lookback(cls, value: str) -> str:
        return check_lookback(value)


class Category(BaseModel):
    key: str
    label: str
    criteria: str
    action: Action
    min_confidence: float = 0.0
    # archive_unsubscribe only: False never unsubscribes when I wrote to the sender or their
    # domain (previous_contact); the email is still archived. Applies on the Jev and rule paths.
    unsubscribe_known_contacts: bool = True


class Tag(BaseModel):
    key: str
    label: str
    question: str
    threshold: float = 0.8


class NeedsReply(BaseModel):
    label: str = "Jev/Needs Reply"
    question: str = "Does the sender expect a reply or an action from me personally?"
    threshold: float = 0.7


class AutoRule(BaseModel):
    hits: int = 3
    min_confidence: float = 0.95
    # An answer only counts when the keep categories (other than the decided one) add up to less
    # than this: senders that mix routine and action-needed mail never earn an archive rule.
    max_keep_probability: float = Field(default=0.02, ge=0.0, le=1.0)


class CategoriesConfig(BaseModel):
    categories: list[Category]
    tags: list[Tag] = Field(default_factory=list)
    needs_reply: NeedsReply = Field(default_factory=NeedsReply)
    review_label: str = "Jev/Review"
    processed_label: str = "Jev/Processed"
    auto_rule: AutoRule = Field(default_factory=AutoRule)
    # Archive when the archive categories' probabilities add up to this, even if the top one
    # alone is below its min_confidence. None disables it.
    archive_group_min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # needs_reply only blocks a group archive when the keep categories (important,
    # action_required) together reach this, unless I have written to the sender or their
    # domain and the email can be replied to (then any reply signal blocks it). 0 = any reply
    # signal blocks it. Must be at most 1 - archive_group_min_confidence: above that the keep
    # categories can never reach it.
    archive_group_reply_keep_probability: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate(self) -> CategoriesConfig:
        keys = [c.key for c in self.categories]
        if len(keys) != len(set(keys)):
            raise ValueError("category keys must be unique")
        if "important" not in keys:
            raise ValueError("a category with key 'important' is required (allowlist target)")
        if len(keys) < 2:
            raise ValueError("at least two categories are required")
        floor = self.archive_group_min_confidence
        # The group only fires when the archive categories reach the floor, so the keep
        # categories are at most 1 - floor; a higher value would silently disable the veto.
        # The epsilon tolerates float rounding (1 - 0.9 == 0.09999999999999998).
        if floor is not None and self.archive_group_reply_keep_probability > 1 - floor + 1e-9:
            raise ValueError(
                "archive_group_reply_keep_probability must be at most "
                "1 - archive_group_min_confidence"
            )
        return self

    @cached_property
    def by_key(self) -> dict[str, Category]:
        return {c.key: c for c in self.categories}

    def keep_probability(
        self, probabilities: dict[str, float], exclude: str | None = None
    ) -> float:
        """Combined probability of the `action: keep` categories (unknown keys are skipped)."""
        return sum(
            p
            for key, p in probabilities.items()
            if key != exclude and key in self.by_key and self.by_key[key].action == Action.KEEP
        )

    def all_labels(self) -> list[str]:
        labels = [c.label for c in self.categories] + [t.label for t in self.tags]
        labels += [self.needs_reply.label, self.review_label, self.processed_label]
        return list(dict.fromkeys(labels))


class Account(BaseModel):
    email: str
    slack_user_id: str | None = None
    mode: Mode | None = None
    digest_language: DigestLanguage = DigestLanguage.EN
    allow_domains: list[str] = Field(default_factory=list)
    allow_senders: list[str] = Field(default_factory=list)
    # Domains whose Google Groups are trusted to name the original author (X-Original-Sender).
    # Opt-in: empty means no group is trusted, not even the account's own domain. Only list
    # domains whose Google Workspace you control.
    group_domains: list[str] = Field(default_factory=list)
    min_confidence: dict[str, float] = Field(default_factory=dict)
    # In act mode, only these category keys get their action; the rest are labeled only.
    # Absent = all categories act; [] = none do. An explicit empty key is rejected (fail closed).
    act_categories: list[str] | None = None

    @field_validator("act_categories", mode="before")
    @classmethod
    def _reject_null_act_categories(cls, value: object) -> object:
        if value is None:
            raise ValueError(
                "act_categories is empty: use [] to act on nothing, or remove the key to act "
                "on every category"
            )
        return value

    @field_validator("group_domains")
    @classmethod
    def _check_group_domains(cls, value: list[str]) -> list[str]:
        return [normalize_group_domain(d) for d in value]

    def is_allowed(self, sender: str) -> bool:
        sender = sender.lower()
        if sender in (s.lower() for s in self.allow_senders):
            return True
        domain = domain_of(sender)
        return any(on_domain(domain, d.lower()) for d in self.allow_domains)


class AccountsConfig(BaseModel):
    accounts: list[Account]


def load_categories(path: Path) -> CategoriesConfig:
    return CategoriesConfig.model_validate(yaml.safe_load(path.read_text()))


def load_accounts(path: Path) -> list[Account]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — copy accounts.example.yaml to {path}")
    return AccountsConfig.model_validate(yaml.safe_load(path.read_text())).accounts
