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
    auto: bool = False                                       # made from world data (no YAML yet): safe defaults, review it

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


# Every country can sign up. A country with its own YAML gets that; any other gets a pack made from world data
# (Unicode CLDR via Babel for currency, languages and time zone; libphonenumber for the calling code) with the
# strictest safe defaults: marketing only with consent, no marketing at night, no tax added, bank transfer to get
# paid. The first business from such a country is flagged to the server admin (see businesses.create).
AUTO_CHANNELS = ["whatsapp", "messenger", "instagram", "sms", "email", "viber", "telegram", "webchat", "webhook"]
AUTO_QUIET = (21, 8)


def _ui_langs() -> tuple[str, ...]:
    from ..web.i18n import UI_LANGS                       # the languages the screens speak
    return UI_LANGS


@lru_cache(maxsize=None)
def _auto(code: str) -> CountryPack:
    import phonenumbers
    from babel.core import get_global
    from babel.languages import get_official_languages
    from babel.numbers import get_territory_currencies
    en = Locale("en")
    cc = phonenumbers.country_code_for_region(code)
    zones = get_global("territory_zones").get(code) or []
    if not zones and cc:                                   # CLDR has no zone (e.g. Kosovo): libphonenumber's
        from phonenumbers import timezone as _pz
        ex = phonenumbers.example_number(code)
        zones = [z for z in (_pz.time_zones_for_number(ex) if ex else ()) if z != "Etc/Unknown"]
    curs = get_territory_currencies(code, tender=True)
    if code not in en.territories or not cc or not zones or not curs:
        raise KeyError(f"no country {code!r}")
    ui = _ui_langs()
    official = [("fil" if x == "tl" else x).split("_")[0] for x in get_official_languages(code, de_facto=True)]
    langs = list(dict.fromkeys([x for x in official if x in ui] + ["en"]))
    locale = "en"                                          # money and dates the local way; plain English if CLDR has no pair
    for cand in [f"{langs[0]}_{code}"] + [f"{x}_{code}" for x in official]:
        try:
            Locale.parse(cand)
            locale = cand
            break
        except Exception:                                  # noqa: BLE001
            pass
    names = {"en": en.territories[code]}
    for lang in ("es",) + tuple(langs):
        try:
            names.setdefault(lang, Locale.parse(lang).territories.get(code) or names["en"])
        except Exception:                                  # noqa: BLE001
            pass
    return CountryPack(code=code, name=names, languages=langs, locale=locale, currency=curs[0], timezone=zones[0],
                       phone_cc=str(cc), tax=Tax(name="Tax", prices_include_tax=True, mode="none"), channels=AUTO_CHANNELS,
                       payments=["bank_transfer"], messaging=Messaging(quiet_hours=AUTO_QUIET, marketing_consent="opt_in"),
                       privacy_law=Law(code="LOCAL", name="the data protection law of " + names["en"]),
                       esign=Law(code="LOCAL", name="the electronic signature law of " + names["en"]), auto=True)


def country(code: str) -> CountryPack:
    code = (code or "").strip().upper()
    try:
        return countries()[code]
    except KeyError:
        pass
    if len(code) == 2 and code.isalpha():
        try:
            return _auto(code)
        except KeyError:
            pass
    raise KeyError(f"no country {code!r}")


def blocked() -> frozenset[str]:
    """Countries under comprehensive sanctions, where no business may sign up (VO_BLOCKED_COUNTRIES overrides;
    the payment, messaging and cloud providers every business relies on can't serve them either)."""
    import os
    raw = os.environ.get("VO_BLOCKED_COUNTRIES")
    return frozenset(x.strip().upper() for x in (raw if raw is not None else "CU,IR,KP,SY").split(",") if x.strip())


def open_codes() -> tuple[str, ...]:
    """The countries a business can sign up from right now (every one, minus the blocked)."""
    b = blocked()
    return tuple(c for c in all_codes() if c not in b)


@lru_cache(maxsize=1)
def all_codes() -> tuple[str, ...]:
    """Every country a business can sign up from: the YAML packs plus every country the world data covers."""
    out = set(countries())
    for code in Locale("en").territories:
        if len(code) == 2 and code.isalpha() and code not in out:
            try:
                _auto(code)
                out.add(code)
            except KeyError:
                pass
    return tuple(sorted(out))


def industry(code: str) -> IndustryPack:
    try:
        return industries()[code.lower()]
    except KeyError:
        raise KeyError(f"no industry pack {code!r}") from None
