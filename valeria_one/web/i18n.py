"""Words on the screens, one JSON file per language in web/locales/.

English (en.json) is the source of truth; any key missing in a language falls back to English. Adding a
language = adding one file (translators: copy en.json, translate the values, keep the keys and {name}).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LOCALES = Path(__file__).parent / "locales"
NATIVE = {"en": "English", "es": "Español", "pt": "Português", "fr": "Français", "de": "Deutsch", "fil": "Filipino",
          "id": "Bahasa Indonesia", "hi": "हिन्दी", "ar": "العربية", "ja": "日本語", "zh": "中文"}
RTL = {"ar", "he", "fa", "ur"}


@lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, str]]:
    return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(LOCALES.glob("*.json"))}


def langs() -> tuple[str, ...]:
    """Every language with a file, English first."""
    have = _load()
    return tuple(["en"] + sorted((k for k in have if k != "en"), key=lambda k: NATIVE.get(k, k)))


UI_LANGS = langs()


class _Table(dict):
    """T[key][lang] view over the files (kept for code and tests that look words up directly)."""

    def __init__(self):
        files = _load()
        super().__init__({k: {lang: files[lang][k] for lang in files if k in files[lang]} for k in files.get("en", {})})


T = _Table()


def t(key: str, lang: str) -> str:
    files = _load()
    return files.get(lang, {}).get(key) or files["en"].get(key) or key


def ui_lang(lang: str | None) -> str:
    """'pt-BR' -> 'pt', 'fil_PH' -> 'fil', unknown -> 'en'."""
    v = (lang or "en").replace("-", "_").split("_")[0].lower()
    if v == "tl":
        v = "fil"                                      # Tagalog browsers say tl
    return v if v in UI_LANGS else "en"


def from_accept_language(header: str | None) -> str | None:
    """The best language we have from the browser's Accept-Language, or None."""
    best = []
    for i, part in enumerate((header or "").split(",")):
        bits = part.strip().split(";q=")
        try:
            q = float(bits[1]) if len(bits) > 1 else 1.0
        except ValueError:
            q = 0.0
        code = bits[0].strip()
        if code and code != "*":
            best.append((-q, i, code))
    for _, _, code in sorted(best):
        v = ui_lang(code)
        base = code.replace("-", "_").split("_")[0].lower()
        if v != "en" or base == "en":
            return v
    return None


def is_rtl(lang: str) -> bool:
    return lang in RTL
