"""«A day with Valeria»: the phone in the hero of the home pages (site01, v0.14.0).

One data file, static/day/day.json, holds the script (the owner's day, step by step) and its words in English,
Español, Taglish, Hinglish and Urdu (the same words as the vid01 videos) and, since glob02 (v0.17.0), Arabic, Indonesian,
Portuguese, French, German, Japanese and Chinese — each with its own made-up shop, names and local prices. static/day.js plays it in the browser;
this module renders the same story's last moments on the server, so the phone shows a finished still frame
before any JavaScript runs, with reduced motion, and to crawlers and link previews. Both draw the same HTML.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

FILE = Path(__file__).parent / "static" / "day" / "day.json"
LANGS = ("en", "es", "tl", "hi", "ur", "ar", "id", "pt", "fr", "de", "ja", "zh")
CHIPS = ("en", "es", "tl", "hi", "ur")                  # the chips every home page shows (plus its own language)
STILL = 5                                               # bubbles the still frame keeps (the chat trims the rest)


@lru_cache(maxsize=1)
def data() -> dict:
    return json.loads(FILE.read_text(encoding="utf-8"))


def keys() -> list[str]:
    return list(data()["strings"]["en"])


def pick(lang: str) -> str:
    return lang if lang in LANGS else "en"


def words(lang: str) -> dict:
    return data()["strings"][pick(lang)]


def wave(n: int = 22) -> str:
    """The voice-note bars (same heights as day.js)."""
    return "".join(f'<i style="height:{6 + round(abs(math.sin(i * 1.7)) * 14)}px"></i>' for i in range(n))


def _btns(w: dict, names: list[str], tapped: str | None) -> str:
    return '<div class="dv-btns">' + "".join(
        f'<span class="dv-btn{" tap" if n == tapped else ""}">{w[n]}</span>' for n in names) + "</div>"


def still(lang: str) -> dict:
    """The finished frame: the clock, the moment and the last bubbles of the day, as day.js would leave them."""
    w = words(lang)
    nodes: list[list] = []                              # [html maker, buttons, tapped]
    clock, moment = "8:00", 0
    for _ms, op, a in data()["script"]:
        if "clock" in a or "clockk" in a:
            clock = a.get("clock") or w[a["clockk"]]
        if "m" in a:
            moment = a["m"]
        if op == "day":
            nodes.append([f'<div class="dv-day">{w[a["k"]]}</div>', None, None])
        elif op == "in":
            nodes.append([f'<div class="dv-b in">{w[a["k"]]}', a.get("btns"), None])
        elif op == "out":
            nodes.append([f'<div class="dv-b out"><small>{w[a["k"]]}</small>', None, None])
        elif op == "voice":
            nodes.append([f'<div class="dv-b out"><div class="dv-voice"><span class="dv-play"></span><div class="dv-wave">{wave()}</div>'
                          f'<small>{a["len"]}</small></div>', None, None])
        elif op == "tap":
            for n in reversed(nodes):
                if n[1] and a["b"] in n[1]:
                    n[2] = a["b"]
                    break
    html = []
    for start, btns, tapped in nodes[-STILL:]:
        if start.startswith('<div class="dv-day"'):
            html.append(start)
        else:
            html.append(start + (_btns(w, btns, tapped) if btns else "") + "</div>")
    return {"clock": clock, "moment": moment, "chat": Markup("".join(html)), "w": {k: Markup(v) for k, v in w.items()},
            "dir": data()["meta"][pick(lang)]["dir"], "video": has_video(lang)}


def has_video(lang: str) -> bool:
    """Is there a «watch the video» MP4 in this language (vid01 made en, es, tl, hi, ur)? Others hide the link."""
    return data()["meta"][pick(lang)].get("video", True)


def chips(lang: str = "en") -> list[tuple[str, str, str]]:
    """(code, name, dir) of the language chips: the page's own language first when it isn't one of the usual five."""
    codes = CHIPS if pick(lang) in CHIPS else (pick(lang),) + CHIPS
    return [(c, data()["meta"][c]["name"], data()["meta"][c]["dir"]) for c in codes]
