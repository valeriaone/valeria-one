"""Readable by AI assistants: /llms.txt, /llms-full.txt and a plain-Markdown twin of every public page.

ChatGPT, Claude, Perplexity and Google's AI answers read the web through crawlers that do best with plain text.
On a server that sells Valeria One (VO_MARKETING=on) every public page also exists as Markdown: add .md to its
address (/index.md for the home page). The twin is made from the page itself on each request, so the two can
never disagree. /llms.txt is the index (what Valeria One is, the price or that it's free, the key pages); /llms-full.txt is every
page's text in one file.
"""
from __future__ import annotations

import re
import time
from html import unescape
from html.parser import HTMLParser

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from .. import config

router = APIRouter(include_in_schema=False)
SKIP = {"head", "style", "script", "nav", "footer", "button", "svg", "noscript", "select", "figure", "dialog"}   # figure: decorative previews and the hero phone (site01)
BLOCK = {"p", "div", "section", "article", "ul", "ol", "table", "h1", "h2", "h3", "h4", "header", "main"}
_FULL: dict = {}                                        # {"at": seconds, "text": str}: /llms-full.txt, kept 10 minutes


class _MD(HTMLParser):
    """Just enough HTML -> Markdown for our own public pages: headings, paragraphs, lists, links, bold, tables."""

    def __init__(self, base: str, url: str = ""):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.base, self.out, self.skip, self.href, self.lists, self.row, self.rows = base, [], 0, None, [], None, 0
        self.title, self.desc, self.in_title = "", "", False

    def _put(self, s: str):
        if self.row:                                    # inside a table cell: keep it in the cell
            self.row[-1] += s
        else:
            self.out.append(s)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta" and a.get("name") == "description":
            self.desc = a.get("content") or ""
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self._put("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._put("\n")
        elif tag == "li":
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            self._put("\n" + ("  " * max(0, len(self.lists) - 1)) + (f"{kind[1]}. " if kind[0] == "ol" else "- "))
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.row.append("")
        elif tag in ("b", "strong"):
            self._put("**")
        elif tag == "a":
            href = a.get("href") or ""
            self.href = (self.base + href) if href.startswith("/") else (self.url + href) if href.startswith("#") else href
            self._put("[")
        elif tag == "label":
            self._put("\n- ")
        elif tag == "br":
            self._put("\n")
        elif tag == "table":
            self.rows = 0
            self._put("\n\n")
        elif tag in BLOCK:
            self._put("\n\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self._put("\n")
        elif tag == "tr" and self.row is not None:
            cells = [" ".join(c.split()).replace("|", "/") for c in self.row]
            self.row = None
            self._put("| " + " | ".join(cells) + " |\n")
            if self.rows == 0:
                self._put("|" + "---|" * len(cells) + "\n")
            self.rows += 1
        elif tag in ("b", "strong"):
            self._put("** ")
        elif tag == "a":
            self._put(f"]({self.href}) " if self.href else "] ")
            self.href = None
        elif tag in BLOCK:
            self._put("\n\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.skip:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            if self.out and not self.out[-1].endswith((" ", "\n")):
                self._put(" ")
            return
        self._put(text)


def to_markdown(html: str, url: str) -> str:
    """One of our pages -> Markdown, with a short header saying where it came from."""
    base = config.PUBLIC_URL()
    p = _MD(base, url)
    p.feed(html)
    body = "".join(p.out)
    body = re.sub(r"\*\* +([^*\n]*?)\*\* ", r"**\1** ", body)
    body = re.sub(r"\*\*\s*\*\* ?", "", body)
    body = re.sub(r"\[\s+", "[", body)
    body = "\n".join(line.strip() if not line.lstrip().startswith(("- ", "  - ")) else line.rstrip() for line in body.split("\n"))
    body = re.sub(r"(?<=\S) {2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    title = " ".join(unescape(p.title).split())
    head = [f"# {title}", ""]
    if p.desc:
        head += [f"> {p.desc}", ""]
    head += [f"Canonical URL: {url}", f"Publisher: Valeria One ({base})", f"Site index: {base}/llms.txt", "", "---", ""]
    return "\n".join(head) + body + "\n"


def public_pages() -> list[str]:
    from . import marketing
    return marketing.pages()


async def _html(request: Request, path: str) -> str | None:
    """Our own page, rendered the normal way (as a first visit in its language), or None."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url=config.PUBLIC_URL()) as c:
        r = await c.get(path, headers={"accept-language": "es" if path.startswith("/mx") else "en"})
    return r.text if r.status_code == 200 and r.headers.get("content-type", "").startswith("text/html") else None


def _on() -> bool:
    from .marketing import on
    return on()


@router.get("/llms.txt")
async def llms(request: Request):
    if not _on():
        return Response("Not found", status_code=404)
    from .marketing import charging, free_words, price_mxn, price_usd
    from .notes import NOTES, note_url
    b = config.PUBLIC_URL()
    if charging():
        price = (f"One plan for the whole team: USD {price_usd()} a month in the United States, MXN {price_mxn()} + IVA a month in Mexico. "
                 f"{free_words()['free_en'].capitalize()}, no contract, no setup fee. ")
    else:
        price = "Free for everyone: everything included, for the whole team, with no card and no contract. "
    other = [p for p in public_pages() if p not in ("/", "/mx") and not p.startswith(("/notes/", "/mx/notas/"))]
    lines = [
        "# Valeria One", "",
        f"> Valeria One is software for running a small business from one place: one inbox for WhatsApp, SMS, email and Telegram, "
        f"missed-call text-back, customers and pipeline, booking, quotes signed on the phone, payment links to the owner's own "
        f"account, reminders and campaigns, Google review requests, win-back messages and bring-a-friend referrals, a website, "
        f"and optional AI with the owner's own key. "
        f"{price}Works in any country, on the business's own "
        f"accounts (WhatsApp, Messenger, Viber, SMS, email, payments). Open source (AGPL-3.0); it can also run on the owner's own server.", "",
        "Every page below has a plain-Markdown twin: add .md to its URL (the home page is /index.md).", "",
        "## Primary links", "",
        f"- Home (United States, English): {b}/",
        f"- Home (Mexico, Spanish): {b}/mx",
        f"- Pricing: {b}/pricing" if charging() else f"- Free, what's included: {b}/pricing",
        f"- Free check-up, how many customers a business is losing (no email): {b}/score — Spanish: {b}/mx/diagnostico",
        f"- Field Notes, practical articles: {b}/notes — Spanish: {b}/mx/notas",
        f"- Our promise, how we describe and offer Valeria One: {b}/promise",
        f"- Start free: {b}/signup",
        "- Live demo with sample businesses: https://sandbox.valai.mx/",
        "- Source code: https://github.com/valeriaone/valeria-one",
        "- Run it on your own server: https://github.com/valeriaone/valeria-one/blob/main/docs/SELF_HOST.md",
        f"- Full text of every page in one file: {b}/llms-full.txt",
        f"- Field Notes feed (RSS): {b}/feed.xml", "",
        "## Quick facts", "",
        "- Product: Valeria One. Created by Tadewan Pascua, Mérida, Yucatán, Mexico. Contact: tad.pascua@valai.mx.",
        "- Works for any kind of small business; setup packs adapt words, stages and services to the business type.",
        "- Languages: English, Spanish, Portuguese, French, German, Japanese, Hindi, Indonesian, Chinese, Arabic, Filipino.",
        "- The business connects its own accounts (email, Twilio number, payment processor, AI key); Valeria One doesn't resell or mark them up.",
        "- Messenger and Instagram connect once Meta approves the app; everything else listed above works today.",
        "- Marketing messages only go to people who agreed; STOP and BAJA are honored right away; US marketing respects quiet hours.", "",
        "## Pages", "",
    ] + [f"- {b}{p}" + (f" (Markdown: {b}{_md_path(p)})") for p in ["/", "/mx"] + other] + [
        "", "## Field Notes", "",
    ] + [f"- [{n['title']}]({b}{note_url(n)}) ({'English' if n['lang'] == 'en' else 'Spanish'}): {n['desc']}" for n in NOTES]
    return PlainTextResponse("\n".join(lines) + "\n")


def _md_path(p: str) -> str:
    return "/index.md" if p == "/" else p + ".md"


@router.get("/llms-full.txt")
async def llms_full(request: Request):
    if not _on():
        return Response("Not found", status_code=404)
    if not _FULL or time.time() - _FULL["at"] > 600:
        b = config.PUBLIC_URL()
        parts = ["# Valeria One: full text", "",
                 f"> Every public page of {b}, as Markdown, in one file. Index: {b}/llms.txt", ""]
        for p in public_pages():
            html = await _html(request, p)
            if html:
                parts += ["", "=" * 80, "", to_markdown(html, b + p)]
        _FULL.update(at=time.time(), text="\n".join(parts))
    return PlainTextResponse(_FULL["text"])


@router.get("/index.md")
async def home_md(request: Request):
    return await _twin(request, "/")


@router.get("/{path:path}.md")
async def page_md(request: Request, path: str):
    return await _twin(request, "/" + path)


async def _twin(request: Request, path: str):
    if not _on() or path not in public_pages():
        return Response("Not found", status_code=404)
    html = await _html(request, path)
    if not html:
        return Response("Not found", status_code=404)
    return Response(to_markdown(html, config.PUBLIC_URL() + path), media_type="text/markdown; charset=utf-8")


AI_BOTS = ["OAI-SearchBot", "GPTBot", "ChatGPT-User", "ClaudeBot", "Claude-User", "Claude-SearchBot", "anthropic-ai",
           "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot", "Applebot-Extended", "Bingbot",
           "Meta-ExternalAgent", "CCBot", "cohere-ai", "DuckAssistBot"]


def robots_txt() -> str:
    b = config.PUBLIC_URL()
    private = ["/app", "/setup", "/join", "/reset", "/q/", "/p/", "/r/", "/stats/", "/pf/"]
    block = "".join(f"Disallow: {p}\n" for p in private)
    out = "User-agent: *\nAllow: /\n" + block
    for bot in AI_BOTS:                                  # AI search and assistants: welcome, same private paths
        out += f"\nUser-agent: {bot}\nAllow: /\n" + block
    out += (f"\n# For AI assistants: {b}/llms.txt (index) · {b}/llms-full.txt (all pages) · add .md to any page for Markdown\n"
            f"Sitemap: {b}/sitemap.xml\n")
    return out
