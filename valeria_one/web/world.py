"""glob02 (v0.17.0): the public website in every language the app speaks.

One home page per language, each at its own short address. The scheme is the app's own language codes
(the same codes as web/locales/*.json and session["lang"]): /fil /hi /ar /id /pt /fr /de /ja /zh. A language
page serves every country that speaks it (Arabic in Dubai, Cairo or Casablanca; Portuguese in São Paulo or Lisbon),
so a country code would wrongly say "one country". Two addresses predate the scheme and stay because links and
tests depend on them: "/" is English and "/mx" is Spanish (/en and /es lead there).

The words live in web/world/<code>.json (English is the source; one file per language, same keys), never in copied
templates. A visitor's explicit choice (the 🌐 picker, or the app's own language picker) is kept in the session
cookie under "lang" — the same key the app reads — so it carries into sign-up and the app, and it beats the browser's
Accept-Language on "/". Crawlers and link previews are never redirected: they get what they asked for.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .i18n import NATIVE, UI_LANGS, from_accept_language, ui_lang

DIR = Path(__file__).parent / "world"
# picker order: the two original homes, then the rest (as Tad listed them)
ORDER = ("en", "es", "fil", "hi", "ar", "id", "pt", "fr", "de", "ja", "zh")
HOMES = {"en": "/", "es": "/mx", **{c: f"/{c}" for c in ORDER[2:]}}
ALIASES = {"/en": "/", "/es": "/mx"}                    # the scheme's own address for the two old homes (301)
HTML_LANG = {"zh": "zh-Hans"}                           # Simplified Chinese (the app's zh.json); everything else = its code
RTL = {"ar"}
DAY = {"fil": "tl"}                                     # the phone animation: Filipino plays the Taglish day
FONTS = {"ja": "Noto Sans JP", "zh": "Noto Sans SC", "hi": "Noto Sans Devanagari", "ar": "Noto Sans Arabic"}
BOTS = re.compile(r"bot\b|bot/|bot;|crawl|spider|slurp|preview|externalhit|externalagent|facebookcatalog|whatsapp|telegram|"
                  r"embedly|inspectiontool|lighthouse|headlesschrome|ia_archiver|mediapartners|feedfetcher|google-extended|"
                  r"anthropic-ai|cohere-ai|perplexity|chatgpt|gptbot|claude|bytespider|petalbot|yandex|baidu|sogou|"
                  r"duckassist|applebot|semrush|ahrefs|mj12|dotbot|validator|pingdom|uptime|monitor", re.I)

assert set(ORDER) == set(UI_LANGS), "every app language has a home page"


@lru_cache(maxsize=None)
def copy(lang: str) -> dict:
    """The raw words of one language (English for a language without a file)."""
    f = DIR / f"{lang}.json"
    return json.loads((f if f.exists() else DIR / "en.json").read_text(encoding="utf-8"))


def html_lang(lang: str) -> str:
    return HTML_LANG.get(lang, lang)


def page_dir(lang: str) -> str:
    return "rtl" if lang in RTL else "ltr"


def day_lang(lang: str) -> str:
    return DAY.get(lang, lang)


def lang_of(path: str) -> str | None:
    """The language whose home page this address is."""
    return next((c for c, p in HOMES.items() if p == path), None)


def words(lang: str, *, free: bool, price: str, free_word: str, n_languages: int) -> dict:
    """The words of a home page, ready to print: the free or the paid lines chosen, {price} {free} {n} filled in."""
    c = dict(copy(lang))
    fill = lambda s: s.replace("{price}", price).replace("{free}", free_word).replace("{n}", str(n_languages))  # noqa: E731
    c["title"] = fill(c["title_free"] if free else c["title_paid"])
    c["lede_full"] = c["lede"] + ("" if lang in ("ja", "zh") else " ") + fill(c["team_free"] if free else c["team_paid"])   # no space between CJK sentences
    faq = [list(x) for x in c["faq"]]
    if not free:
        faq[1] = [fill(x) for x in c["faq_paid"]]
    c["faq_now"] = [tuple(x) for x in faq]
    for k in ("hero_k", "langs_small", "cost_paid", "per_paid"):
        c[k] = fill(c[k])
    return c


def free_word(lang: str, launch_months: int | None, charging: bool) -> str:
    """"Free" / "12 months free" / "30 days free" in the page's language (same words as marketing.free_words for en, es)."""
    c = copy(lang)
    if not charging:
        return c["free_word"]
    return c["free_months"].replace("{n}", str(launch_months)) if launch_months else c["free_days"]


# ---------------------------------------------------------------- who is asking, and what they chose
def is_bot(request) -> bool:
    """Crawlers, link previews and monitors (or no user agent at all): never redirected by language."""
    ua = request.headers.get("user-agent") or ""
    return not ua.strip() or bool(BOTS.search(ua))


def remember(request) -> str | None:
    """?hl=<code> is an explicit choice (the 🌐 picker): keep it in the session, under the key the app reads."""
    code = (request.query_params.get("hl") or "").strip()
    if code in UI_LANGS:
        request.session["lang"] = code
        return code
    return None


def wanted(request) -> str:
    """The language this person wants: their choice (session), else their browser, else English."""
    if request.session.get("lang"):
        return ui_lang(request.session["lang"])
    return from_accept_language(request.headers.get("accept-language")) or "en"


# ---------------------------------------------------------------- the 🌐 picker and the alternates
def picker(current: str, targets: dict | None = None, here: str | None = None) -> list[dict]:
    """The 11 languages, each in its own name. `targets` = this page in other languages (else their home pages);
    `here` = the same address in every language (pages on base.html, which speak the visitor's language)."""
    out = []
    for c in ORDER:
        path = here or (targets or {}).get(c) or HOMES[c]
        out.append({"code": c, "name": NATIVE[c], "href": f"{path}?hl={c}", "lang": html_lang(c), "dir": page_dir(c),
                    "current": c == current})
    return out


def alternates() -> list[tuple[str, str]]:
    """(hreflang, path) for every home page, plus x-default (English, which also picks by browser)."""
    return [(html_lang(c), HOMES[c]) for c in ORDER] + [("x-default", "/")]


def homes() -> list[str]:
    return [HOMES[c] for c in ORDER]
