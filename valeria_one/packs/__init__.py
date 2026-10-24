"""Packs: everything that changes from one country or one industry to another lives here, as data.

A business is a combination of one country pack and one industry pack. The core code never asks
"is this Mexico?" or "is this a salon?" — it asks the pack. Adding a country or an industry means
adding a YAML file, not touching code.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from babel import Locale
from babel.numbers import get_currency_precision
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HERE = Path(__file__).parent
CHANNELS = ("whatsapp", "messenger", "instagram", "sms", "email", "viber", "line", "telegram", "webchat", "webhook", "sandbox")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Tax(_Strict):
    name: str
    rate_bp: int | None = Field(None, ge=0, le=10_000)     # basis points: 1600 = 16 %
    prices_include_tax: bool
    mode: Literal["fixed", "external", "none"]               # external = computed by a tax service (US sales tax, BR)

    @model_validator(mode="after")
    def _rate_matches_mode(self):
        if self.mode == "fixed" and self.rate_bp is None:
            raise ValueError("a fixed tax needs rate_bp")
        if self.mode != "fixed" and self.rate_bp is not None:
            raise ValueError("only a fixed tax has rate_bp")
        return self


class Messaging(_Strict):
    quiet_hours: tuple[int, int] | None = None               # (from, to) local hours when marketing may NOT go out
    marketing_consent: Literal["opt_in", "double_opt_in", "soft_opt_in", "prior_express_written"]
    sms_registration: str | None = None                      # e.g. a2p_10dlc (US), trai_dlt (IN)


class Law(_Strict):
    code: str
    name: str


class CountryPack(_Strict):
    code: str = Field(pattern=r"^[A-Z]{2}$")
    name: dict[str, str]
    languages: list[str] = Field(min_length=1)
    locale: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    timezone: str
    phone_cc: str = Field(pattern=r"^[0-9]{1,3}$")
    tax: Tax
    channels: list[str] = Field(min_length=1)
    payments: list[str]
    messaging: Messaging
    privacy_law: Law
    esign: Law
    invoicing: Law | None = None
    data_residency: str | None = None
    reviewed_by_counsel: bool = False                        # legal fields are a starting point, not legal advice

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        ZoneInfo(v)
        return v

    @field_validator("locale")
    @classmethod
    def _locale(cls, v):
        Locale.parse(v)
        return v

    @field_validator("channels")
    @classmethod
    def _channels(cls, v):
        bad = [c for c in v if c not in CHANNELS]
        if bad:
            raise ValueError(f"unknown channels {bad}")
        return v

    @property
    def currency_digits(self) -> int:
        return get_currency_precision(self.currency)

    def label(self, lang: str) -> str:
        return self.name.get(lang) or self.name["en"]


class Stage(_Strict):
    key: str = Field(pattern=r"^[a-z_]+$")
    outcome: Literal["open", "won", "lost"]
    label: dict[str, str]


class IndustryPack(_Strict):
    code: str = Field(pattern=r"^[a-z_]+$")
    name: dict[str, str]
    words: dict[str, dict[str, str]]
    stages: list[Stage] = Field(min_length=2)

    @model_validator(mode="after")
    def _stages_ok(self):
        keys = [s.key for s in self.stages]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate stage keys")
        outcomes = {s.outcome for s in self.stages}
        if not {"won", "lost"} <= outcomes:
            raise ValueError("an industry needs a won stage and a lost stage")
        if self.stages[0].outcome != "open":
            raise ValueError("the first stage must be open")
        for w in ("contact", "deal", "appointment"):
            if "en" not in self.words.get(w, {}):
                raise ValueError(f"word {w!r} needs an English label")
        return self

    def stage(self, key: str) -> Stage | None:
        return next((s for s in self.stages if s.key == key), None)

    @property
    def first_stage(self) -> str:
        return self.stages[0].key


def text(labels: dict[str, str], lang: str) -> str:
    """The label in this language, else English."""
    return labels.get(lang) or labels.get((lang or "").split("_")[0]) or labels["en"]


def _load(folder: str, model):
    out = {}
    for f in sorted((HERE / folder).glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        pack = model(**data)
        if pack.code != f.stem:
            raise ValueError(f"{f.name}: code {pack.code!r} must match the file name")
        out[pack.code] = pack
    return out


@lru_cache(maxsize=1)
def countries() -> dict[str, CountryPack]:
    return _load("countries", CountryPack)


@lru_cache(maxsize=1)
def industries() -> dict[str, IndustryPack]:
    return _load("industries", IndustryPack)


def country(code: str) -> CountryPack:
    try:
        return countries()[code.upper()]
    except KeyError:
        raise KeyError(f"no country pack {code!r}") from None


def industry(code: str) -> IndustryPack:
    try:
        return industries()[code.lower()]
    except KeyError:
        raise KeyError(f"no industry pack {code!r}") from None
