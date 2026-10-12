"""Runtime settings (.env) plus the accounts and categories YAML files."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Gmail `newer_than:` units: days, months, years.
LOOKBACK_RE = re.compile(r"^\d+[dmy]$")


def check_lookback(value: str) -> str:
    if not LOOKBACK_RE.match(value):
        raise ValueError(f"invalid lookback {value!r}: use a number plus d, m or y, e.g. 2d or 30d")
    return value


class Mode(StrEnum):
    DRY_RUN = "dry_run"
    LABEL = "label"
    ACT = "act"


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

    @model_validator(mode="after")
    def _validate(self) -> CategoriesConfig:
        keys = [c.key for c in self.categories]
        if len(keys) != len(set(keys)):
            raise ValueError("category keys must be unique")
        if "important" not in keys:
            raise ValueError("a category with key 'important' is required (allowlist target)")
        if len(keys) < 2:
            raise ValueError("at least two categories are required")
        return self

    @cached_property
    def by_key(self) -> dict[str, Category]:
        return {c.key: c for c in self.categories}

    def all_labels(self) -> list[str]:
        labels = [c.label for c in self.categories] + [t.label for t in self.tags]
        labels += [self.needs_reply.label, self.review_label, self.processed_label]
        return list(dict.fromkeys(labels))


class Account(BaseModel):
    email: str
    slack_user_id: str | None = None
    mode: Mode | None = None
    allow_domains: list[str] = Field(default_factory=list)
    allow_senders: list[str] = Field(default_factory=list)
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

    def is_allowed(self, sender: str) -> bool:
        sender = sender.lower()
        domain = sender.rsplit("@", 1)[-1]
        if sender in (s.lower() for s in self.allow_senders):
            return True
        return any(
            domain == d.lower() or domain.endswith("." + d.lower()) for d in self.allow_domains
        )


class AccountsConfig(BaseModel):
    accounts: list[Account]


def load_categories(path: Path) -> CategoriesConfig:
    return CategoriesConfig.model_validate(yaml.safe_load(path.read_text()))


def load_accounts(path: Path) -> list[Account]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — copy accounts.example.yaml to {path}")
    return AccountsConfig.model_validate(yaml.safe_load(path.read_text())).accounts
