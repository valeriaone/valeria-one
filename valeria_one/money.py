"""Money: always integers in the currency's smallest unit (cents, centavos, yen). Never floats.

Tax comes from the country pack: a fixed rate (IVA, VAT, GST) is computed here; an external tax
(US sales tax, Brazil) is left to the payment provider's tax service and reported as unknown (None).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from babel.numbers import format_currency, get_currency_precision

from .packs import CountryPack


def _half_up(n: int, d: int) -> int:
    """Integer division rounded half away from zero (how tax authorities round)."""
    return int((Decimal(n) / Decimal(d)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Price:
    net_minor: int                  # before tax
    tax_minor: int | None           # None = computed elsewhere (external tax)
    gross_minor: int | None         # what the customer pays; None when tax is external
    currency: str
    tax_name: str


def price(amount_minor: int, pack: CountryPack, *, includes_tax: bool | None = None) -> Price:
    """Split an amount into net + tax + gross for this country.

    includes_tax: whether amount_minor already contains the tax. Defaults to the pack's display rule.
    """
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise TypeError("amount_minor must be an int (smallest currency unit)")
    if amount_minor < 0:
        raise ValueError("amount_minor must be >= 0")
    t = pack.tax
    inc = t.prices_include_tax if includes_tax is None else includes_tax
    if t.mode != "fixed":
        if inc:
            return Price(None, None, amount_minor, pack.currency, t.name)  # type: ignore[arg-type]
        return Price(amount_minor, None, None, pack.currency, t.name)
    bp = t.rate_bp
    if inc:
        net = _half_up(amount_minor * 10_000, 10_000 + bp)
        return Price(net, amount_minor - net, amount_minor, pack.currency, t.name)
    tax = _half_up(amount_minor * bp, 10_000)
    return Price(amount_minor, tax, amount_minor + tax, pack.currency, t.name)


def fmt(amount_minor: int, currency: str, locale: str) -> str:
    """Format for people: fmt(24900, 'MXN', 'es_MX') -> '$249.00'."""
    digits = get_currency_precision(currency)
    return format_currency(Decimal(amount_minor).scaleb(-digits), currency, locale=locale)


def to_major(amount_minor: int | None, currency: str) -> str:
    """For a form field: 24950 -> '249.5' for MXN, 1200 -> '1200' for JPY; None -> ''."""
    if amount_minor is None:
        return ""
    v = Decimal(amount_minor).scaleb(-get_currency_precision(currency))
    return format(v.normalize(), "f") if v != v.to_integral_value() else str(int(v))


def to_minor(amount: str | int | Decimal, currency: str) -> int:
    """'249.50' -> 24950 for MXN; '1200' -> 1200 for JPY. Rejects more decimals than the currency has."""
    digits = get_currency_precision(currency)
    d = Decimal(str(amount))
    scaled = d.scaleb(digits)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{currency} has {digits} decimals")
    return int(scaled)
