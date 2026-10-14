"""Runtime settings (.env) plus the accounts and categories YAML files."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .psl import is_public_suffix
from .signals import ADDRESS, DOMAIN, domain_of, on_domain

# A probability or confidence threshold: Jev's probabilities are between 0 and 1.
Probability = Annotated[float, Field(ge=0.0, le=1.0)]

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


def normalize_sender_key(value: str) -> str | None:
    """`value` as a sender key, stripped and lowercased: an address (ana@example.com) or
    `@domain` (@example.com). None when it is neither (a bare domain, a display name, a typo)."""
    key = value.strip().lower()
    if ADDRESS.match(key) or (key.startswith("@") and DOMAIN.match(key[1:])):
        return key
    return None


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


class LogLevel(StrEnum):
    """LOG_LEVEL: a `logging` level name, accepted in any case (`info`, `Info`)."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


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
    max_messages_per_run: int = Field(default=200, ge=1)
    max_unsubscribes_per_run: int = Field(default=20, ge=0)  # 0 never unsubscribes
    body_chars: int = Field(default=3000, ge=1)
    log_level: LogLevel = LogLevel.INFO

    @field_validator("lookback")
    @classmethod
    def _check_lookback(cls, value: str) -> str:
        return check_lookback(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class Category(BaseModel):
    key: str
    label: str
    criteria: str
    action: Action
    min_confidence: Probability = 0.0
    # archive_unsubscribe only: False never unsubscribes when I wrote to the sender or their
    # domain (previous_contact); the email is still archived. Applies on the Jev and rule paths.
    unsubscribe_known_contacts: bool = True


class Tag(BaseModel):
    key: str
    label: str
    question: str
    threshold: Probability = 0.8


class NeedsReply(BaseModel):
    label: str = "Jev/Needs Reply"
    question: str = "Does the sender expect a reply or an action from me personally?"
    threshold: Probability = 0.7


class AutoRule(BaseModel):
    hits: int = Field(default=3, ge=1)
    min_confidence: Probability = 0.95
    # An answer only counts when the keep categories (other than the decided one) add up to less
    # than this: senders that mix routine and action-needed mail never earn an archive rule.
    max_keep_probability: Probability = 0.02


class CategoriesConfig(BaseModel):
    categories: list[Category]
    tags: list[Tag] = Field(default_factory=list)
    needs_reply: NeedsReply = Field(default_factory=NeedsReply)
    review_label: str = "Jev/Review"
    processed_label: str = "Jev/Processed"
    auto_rule: AutoRule = Field(default_factory=AutoRule)
    # Archive when the archive categories' probabilities add up to this, even if the top one
    # alone is below its min_confidence. None disables it.
    archive_group_min_confidence: Probability | None = None
    # needs_reply only blocks a group archive when the keep categories (important,
    # action_required) together reach this, unless I have written to the sender or their
    # domain and the email can be replied to (then any reply signal blocks it). 0 = any reply
    # signal blocks it. Must be at most 1 - archive_group_min_confidence: above that the keep
    # categories can never reach it.
    archive_group_reply_keep_probability: Probability = 0.05

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

    @cached_property
    def by_label(self) -> dict[str, str]:
        """Category key by its Gmail label."""
        return {c.label: c.key for c in self.categories}

    @cached_property
    def managed_labels(self) -> frozenset[str]:
        """Every label we set (`all_labels`) except the processed label."""
        return frozenset(self.all_labels()) - {self.processed_label}

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
    # Senders Jev decides email by email, for platforms that send both mail I can archive and
    # mail that needs me (a CRM's notifications address sends dashboards and website leads):
    # no automatic or correction rule is learned for them, and only a manual rule (`rules set`)
    # applies. Addresses or @domain (subdomains included), as `rules set` takes them.
    decide_each_email: list[str] = Field(default_factory=list)
    # Keys must be categories in categories.yaml (`validate_accounts`).
    min_confidence: dict[str, Probability] = Field(default_factory=dict)
    # In act mode, only these category keys get their action; the rest are labeled only.
    # Absent = all categories act; [] = none do. An explicit empty key is rejected (fail closed).
    # Keys must be categories in categories.yaml (`validate_accounts`).
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

    @field_validator("allow_senders", "allow_domains")
    @classmethod
    def _normalize_allowlist(cls, value: list[str]) -> list[str]:
        """Matched case-insensitively: stored lowercased (and stripped) once, not per email."""
        return [entry.strip().lower() for entry in value]

    @field_validator("decide_each_email")
    @classmethod
    def _check_decide_each_email(cls, value: list[str]) -> list[str]:
        keys = []
        for entry in value:
            key = normalize_sender_key(entry)
            if key is None:
                raise ValueError(
                    f"decide_each_email entry {entry!r} is neither an address "
                    "(ana@example.com) nor @domain (@example.com)"
                )
            keys.append(key)
        return keys

    def decides_each_email(self, sender: str) -> bool:
        """`sender` is in decide_each_email: by address, or on a listed @domain (subdomains
        included). Its sender rules don't decide, except a manual one (`rules.SenderRules`)."""
        sender = sender.lower()
        domain = domain_of(sender)
        return any(
            on_domain(domain, key[1:]) if key.startswith("@") else sender == key
            for key in self.decide_each_email
        )

    def matches_allowlist(self, sender: str) -> bool:
        """`sender` is in allow_senders or on a domain in allow_domains (subdomains included).

        The author only: `policy.is_allowed` also counts a group listed in allow_senders.
        """
        sender = sender.lower()
        if sender in self.allow_senders:
            return True
        domain = domain_of(sender)
        return any(on_domain(domain, d) for d in self.allow_domains)


class AccountsConfig(BaseModel):
    accounts: list[Account]


class ConfigError(ValueError):
    """The accounts file doesn't fit the categories file (see `validate_accounts`)."""


def validate_accounts(accounts: list[Account], cfg: CategoriesConfig) -> None:
    """Raise ConfigError listing every `act_categories` and `min_confidence` key, of every
    account, that isn't a category in `cfg`.

    The two files load separately, so each model can't check this alone. An unknown key would
    otherwise be ignored: a typo in `act_categories` would silently leave that category
    labeled only, and one in `min_confidence` would leave the category on its default threshold.
    """
    problems: list[str] = []
    for account in accounts:
        for field, keys in (
            ("act_categories", account.act_categories or []),
            ("min_confidence", account.min_confidence),
        ):
            unknown = sorted(set(keys) - set(cfg.by_key))
            if unknown:
                problems.append(f"{account.email}: unknown {field} {unknown}")
    if problems:
        raise ConfigError(
            "accounts use categories that aren't in the categories file "
            f"(known: {', '.join(cfg.by_key)}):\n" + "\n".join(f"  {p}" for p in problems)
        )


def load_categories(path: Path) -> CategoriesConfig:
    return CategoriesConfig.model_validate(yaml.safe_load(path.read_text()))


def find_account(accounts: list[Account], email: str) -> Account | None:
    """The account whose address is `email`, ignoring case, or None."""
    email = email.lower()
    return next((a for a in accounts if a.email.lower() == email), None)


def load_accounts(path: Path) -> list[Account]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — copy accounts.example.yaml to {path}")
    return AccountsConfig.model_validate(yaml.safe_load(path.read_text())).accounts
