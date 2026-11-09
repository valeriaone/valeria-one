"""help01 (Tad 2026-11-09 «valeria should be there to assist clients … click and type questions … guide setup wizard step by step …
be proactive»): Valeria's built-in help inside the office — no AI key needed.

GUIDES: each common job (connect WhatsApp, prices, hours, my page, photos, payments, AI key, booking link, team) with keywords to
find it, a short answer, and the steps of its wizard (the page, the spot on it to point at, what to say). `match(text)` finds the
guides a question is about; `for_page(path)` the guide for the page someone is stuck on; `ask_for_me` hands a job to valAI
(owner_asks → Tad's alert) when the owner says «do it for me». EN + ES (Tad 2026-11-08); other languages get English.
"""
from __future__ import annotations

import re
import unicodedata
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.engine import Connection

from .. import db

# key: (title, keywords, answer, steps[(path, css selector, text)]) — per language
GUIDES: dict[str, dict] = {
    "whatsapp": {
        "words": "whatsapp wasap whats numero número conectar connect mensajes messages chat waba twilio meta telefono phone",
        "en": ("Connect your WhatsApp", "Your customers write to your own WhatsApp number and I answer them in your office. You connect it once with your own account (WhatsApp Business through Twilio or Meta). Want me to walk you through it?",
               [("/app/channels", "#manual", "Choose «WhatsApp · Twilio» (or your WhatsApp Business), paste the keys from your own account and Save."),
                ("/app/channels", "#manual", "Done: your customers' messages arrive here and I answer them. Stuck? Tap «Do it for me» and we finish it together.")]),
        "es": ("Conectar su WhatsApp", "Sus clientes le escriben a su propio número de WhatsApp y yo les contesto desde su oficina. Se conecta una sola vez, con su propia cuenta (WhatsApp Business por Twilio o Meta). ¿Le enseño paso a paso?",
               [("/app/channels", "#manual", "Elija «WhatsApp · Twilio» (o su WhatsApp Business), pegue las claves de su propia cuenta y Guardar."),
                ("/app/channels", "#manual", "Listo: los mensajes de sus clientes llegan aquí y yo los contesto. ¿Se atoró? Toque «Hágalo por mí» y lo terminamos juntos.")]),
    },
    "prices": {
        "words": "precio precios price prices servicio servicios service services costo cost tarifa menu menú cuanto cuánto lista catalogo catálogo",
        "en": ("Load your prices", "Your prices go in your services: name, how long it takes and the price. I quote from them on WhatsApp and on your booking page. Send me a photo of your price list and I can load it for you too.",
               [("/app/calendar", "form[action='/app/booking/services']", "Add each service here: name, minutes and price, then tap Add."),
                ("/app/calendar", "#booking", "That's it: these prices show on your booking page and I use them to answer.")]),
        "es": ("Cargar sus precios", "Sus precios van en sus servicios: nombre, cuánto dura y el precio. Con eso cotizo por WhatsApp y en su página de citas. Si prefiere, mándeme una foto de su lista y yo la cargo.",
               [("/app/calendar", "form[action='/app/booking/services']", "Agregue aquí cada servicio: nombre, minutos y precio, y toque Agregar."),
                ("/app/calendar", "#booking", "Listo: estos precios salen en su página de citas y con ellos contesto.")]),
    },
    "hours": {
        "words": "horario horarios hours hora abierto open cerrado closed agenda citas booking reservar dias días",
        "en": ("Set your hours", "Your opening hours decide when customers can book with you. Set them once and I only offer times you're open.",
               [("/app/calendar", "form[action='/app/booking']", "Set your days and hours here, then Save. Customers only see free times inside them.")]),
        "es": ("Poner su horario", "Su horario decide cuándo le pueden agendar. Lo pone una vez y yo solo ofrezco horas en que usted abre.",
               [("/app/calendar", "form[action='/app/booking']", "Ponga aquí sus días y horas, y Guardar. Sus clientes solo ven horas libres dentro de ese horario.")]),
    },
    "page": {
        "words": "pagina página web website sitio site editar edit cambiar change texto text color logo titulo título look diseño",
        "en": ("Edit your page", "Your website is in Website: headline, about you, address, contact, your social links and your color. What you save shows on your page right away.",
               [("/app/website", "input[name='headline']", "Your headline: one line that says what you do and where."),
                ("/app/website", "textarea[name='about'], input[name='about']", "About you: what you sell, how you work, why customers choose you."),
                ("/app/website", "#look", "Pick how it looks and your color, then Save. You see it live on the right.")]),
        "es": ("Editar su página", "Su página está en Página web: título, quién es, dirección, contacto, sus redes y su color. Lo que guarda sale en su página al instante.",
               [("/app/website", "input[name='headline']", "Su título: una línea que diga qué hace y dónde."),
                ("/app/website", "textarea[name='about'], input[name='about']", "Quién es: qué vende, cómo trabaja, por qué lo eligen."),
                ("/app/website", "#look", "Elija cómo se ve y su color, y Guardar. La ve en vivo a la derecha.")]),
    },
    "photos": {
        "words": "foto fotos photo photos imagen imagenes imágenes picture pictures galeria galería trabajos portfolio subir upload video videos",
        "en": ("Put photos and a video on your page", "In My page, tap «Add to my page» and choose photos (they go to «Our work») or a short video (MP4/MOV up to 25 MB, it plays at the top). A YouTube or TikTok link works too. Or just send it to me on WhatsApp saying «put it on my page».",
               [("/app/website", "#media", "Choose your photos or a video here and tap «Add to my page». They show on your page right away."),
                ("/app/website", "input[name='video_url']", "Have a YouTube or TikTok video? Paste its link here and Save.")]),
        "es": ("Poner fotos y video en su página", "En Mi página toque «Añadir a mi página» y elija fotos (van a «Nuestro trabajo») o un video corto (MP4/MOV hasta 25 MB, se reproduce arriba). También sirve una liga de YouTube o TikTok. O mándemela por WhatsApp diciendo «ponla en mi página».",
               [("/app/website", "#media", "Elija aquí sus fotos o un video y toque «Añadir a mi página». Salen en su página al instante."),
                ("/app/website", "input[name='video_url']", "¿Tiene un video en YouTube o TikTok? Pegue aquí su liga y Guardar.")]),
    },
    "payments": {
        "words": "pago pagos cobrar cobro payment payments pay stripe mercado pago mercadopago tarjeta card anticipo deposit link liga",
        "en": ("Get paid", "You get paid in your own account (Stripe, Mercado Pago and more). Connect it once and I send payment links and deposits for you. We never touch your money.",
               [("/app/settings#payments", "#payments", "Choose your payment account here and paste its keys. From then on I can send payment links.")]),
        "es": ("Cobrar", "Cobra en su propia cuenta (Mercado Pago, Stripe y más). La conecta una vez y yo mando ligas de pago y anticipos por usted. Nunca tocamos su dinero.",
               [("/app/settings#payments", "#payments", "Elija aquí su cuenta de cobro y pegue sus claves. Desde ese momento puedo mandar ligas de pago.")]),
    },
    "ai": {
        "words": "ia ai inteligencia artificial gemini llave key clave chatgpt openai claude responder sola automatico automático",
        "en": ("Let me answer by myself", "To write answers on my own I use your own AI key. Google Gemini has a free one: aistudio.google.com/apikey. Paste it in Settings › AI and choose how I work.",
               [("/app/settings#ai", "#ai", "Paste your AI key here (Gemini is free) and Save. Then I can think and answer for you.")]),
        "es": ("Que yo conteste sola", "Para escribir respuestas por mi cuenta uso su propia llave de IA. Google Gemini tiene una gratis: aistudio.google.com/apikey. Péguela en Ajustes › IA y elija cómo trabajo.",
               [("/app/settings#ai", "#ai", "Pegue aquí su llave de IA (Gemini es gratis) y Guardar. Desde ahí pienso y contesto por usted.")]),
    },
    "link": {
        "words": "liga link enlace compartir share citas reservas booking agendar instagram bio qr",
        "en": ("Share your booking link", "Your booking page is your link: put it in your Instagram bio, WhatsApp status and Google. Customers pick a time and I confirm and remind them.",
               [("/app/calendar", "#booking", "Your booking link is here. Copy it and put it everywhere your customers find you.")]),
        "es": ("Compartir su liga de citas", "Su página de citas es su liga: póngala en su bio de Instagram, su estado de WhatsApp y Google. Sus clientes eligen hora y yo confirmo y les recuerdo.",
               [("/app/calendar", "#booking", "Aquí está su liga de citas. Cópiela y póngala donde sus clientes lo encuentran.")]),
    },
    "team": {
        "words": "equipo team empleado empleados staff usuario usuarios invitar invite socio colaborador",
        "en": ("Invite your team", "Your whole team is included. Invite each person by email; they get their own login.",
               [("/app/settings#team", "#team", "Type their email here and invite. They get a link to come in.")]),
        "es": ("Invitar a su equipo", "Todo su equipo está incluido. Invite a cada persona por correo; cada quien entra con su propio usuario.",
               [("/app/settings#team", "#team", "Escriba aquí su correo e invite. Les llega una liga para entrar.")]),
    },
}

TEXT = {
    "en": {"none": "I can help with: connecting WhatsApp, prices, hours, your page, photos, payments, AI, your booking link and your team. Tap one, or say «do it for me» and we'll do it for you.",
           "go": "Let's do it together", "forme": "Do it for me", "next": "Next", "done": "Done ✓", "later": "Not now", "step": "Step",
           "stuck": "Need a hand? I can show you step by step, or do it for you.", "errors": "That didn't go through twice. Want me to help?",
           "asked": "Got it ✓ We'll do it for you today and let you know here.", "go_to": "Go to the next step"},
    "es": {"none": "Le ayudo con: conectar WhatsApp, precios, horario, su página, fotos, cobros, IA, su liga de citas y su equipo. Toque uno, o diga «hágalo por mí» y lo hacemos nosotros.",
           "go": "Hagámoslo juntos", "forme": "Hágalo por mí", "next": "Siguiente", "done": "Listo ✓", "later": "Ahora no", "step": "Paso",
           "stuck": "¿Le echo la mano? Le enseño paso a paso, o lo hago yo por usted.", "errors": "Eso no pasó dos veces. ¿Le ayudo?",
           "asked": "Listo ✓ Se lo dejamos hecho hoy y le avisamos aquí.", "go_to": "Ir al siguiente paso"},
}


def lang_of(lang: str | None) -> str:
    return "es" if (lang or "").lower().startswith("es") else "en"


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def match(text: str, n: int = 3) -> list[str]:
    """The guides a question is about, best first (keyword hits, accents and case ignored)."""
    words = set(re.findall(r"[a-z0-9]+", _norm(text)))
    score = []
    for key, g in GUIDES.items():
        hits = sum(1 for w in set(re.findall(r"[a-z0-9]+", _norm(g["words"]))) if w in words or (len(w) > 4 and any(x.startswith(w[:5]) for x in words)))
        if hits:
            score.append((hits, key))
    return [k for _, k in sorted(score, key=lambda x: -x[0])[:n]]


def for_page(path: str) -> str | None:
    """The guide whose first step is on this page (for «stuck here?»)."""
    p = (path or "").split("?")[0].split("#")[0]
    for key, g in GUIDES.items():
        if any(s[0].split("#")[0] == p for s in g["en"][2]):
            return key
    return None


def catalog(lang: str) -> dict:
    """Everything the office page needs, in one language: guides (title, answer, steps) and the words of the help panel."""
    L = lang_of(lang)
    return {"guides": {k: {"title": g[L][0], "answer": g[L][1], "steps": [{"path": p, "sel": s, "text": t} for p, s, t in g[L][2]]}
                       for k, g in GUIDES.items()},
            "text": TEXT[L]}


def answer(text: str, lang: str) -> dict:
    """The built-in answer (no AI key needed): the best guide's answer + up to 3 guides to start, or the menu of what I help with."""
    L, keys = lang_of(lang), match(text)
    if not keys:
        return {"say": TEXT[L]["none"], "guides": list(GUIDES)[:6], "actions": []}
    return {"say": GUIDES[keys[0]][L][1], "guides": keys, "actions": []}


def ask_for_me(conn: Connection, biz: dict, user_id: UUID | None, task: str, page: str) -> None:
    """«Do it for me»: the job goes to valAI as an owner ask (Tad is emailed by the claims alert) — we do it and tell them."""
    t = " ".join((task or "").split())[:300] or "help"
    title = GUIDES[t][lang_of("es")][0] if t in GUIDES else t
    conn.execute(insert(db.owner_asks).values(business_id=biz["id"], user_id=user_id, industry=biz.get("industry") or "generic",
                                              text=f"[Hágalo por mí] {title} · {(page or '')[:120]}", status="new"))
