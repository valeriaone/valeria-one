"""Phone numbers in E.164 (+5299912345678), using the business's country as the default region."""
from __future__ import annotations

import re

import phonenumbers

from .packs import CountryPack


class BadPhone(ValueError):
    pass


# Old dialing prefixes that people (and WhatsApp) still send. Mexico dropped the mobile "1" in 2019,
# but WhatsApp webhooks still arrive as +521… — {country code: (legacy digit, national length after it)}.
_LEGACY = {"52": ("1", 10)}


def _drop_legacy(s: str) -> str:
    digits = re.sub(r"\D", "", s)
    for cc, (extra, n) in _LEGACY.items():
        if digits.startswith(cc + extra) and len(digits) == len(cc) + 1 + n:
            return "+" + cc + digits[len(cc) + 1:]
    return s


def e164(raw: str, pack: CountryPack) -> str:
    """Any way a person types a number -> +<cc><number>. Raises BadPhone if it isn't a real number."""
    s = (raw or "").strip()
    if not s:
        raise BadPhone("empty")
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+"):
        s = _drop_legacy(s)
    try:
        n = phonenumbers.parse(s, None if s.startswith("+") else pack.code)
    except phonenumbers.NumberParseException as e:
        raise BadPhone(str(e)) from None
    if not phonenumbers.is_valid_number(n):
        raise BadPhone(f"not a valid number for {phonenumbers.region_code_for_number(n) or pack.code}")
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


def display(e164_number: str, pack: CountryPack) -> str:
    """National format at home, international format for numbers from elsewhere."""
    n = phonenumbers.parse(e164_number, None)
    home = phonenumbers.region_code_for_number(n) == pack.code
    fmt = phonenumbers.PhoneNumberFormat.NATIONAL if home else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    return phonenumbers.format_number(n, fmt)
