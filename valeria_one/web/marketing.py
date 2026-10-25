"""The public pages of a server that sells Valeria One (VO_MARKETING=on, e.g. cloud.valai.mx): the home page
(English) and /mx (Spanish), and one page per search a ready-to-buy owner makes — «CRM for HVAC», «missed
call text back», «Podium alternative», «CRM para estéticas». Moved here from one.valai.mx (2026-10-24) and
rewritten so every line is true of this product: the real price, what works today, what is coming, and what
the big tools do better (competitor prices from the October 2026 research).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .. import config
from . import aisearch, notes

router = APIRouter(include_in_schema=False)
DEMO = "https://sandbox.valai.mx/#demos"


def on() -> bool:
    return (config.get("VO_MARKETING") or "off").lower() == "on" and not config.SANDBOX()


def price_usd() -> str:
    from ..services import billing
    return billing.fmt(billing.prices()["starter"]["USD"], "USD").replace(" USD", "").removesuffix(".00")


def price_mxn() -> str:
    from ..services import billing
    return billing.fmt(billing.prices()["starter"]["MXN"], "MXN").replace(" MXN", "").removesuffix(".00")


# ---------------------------------------------------------------- what works today (both home pages use it)
TOOLS_EN = [
    ("One inbox", "WhatsApp, SMS, email, Telegram and any system by webhook, in one list. Messenger and Instagram as soon as Meta approves our app."),
    ("Missed-call text-back", "When you can't pick up, the caller gets a text from your business number in seconds."),
    ("Customers and pipeline", "One record per person, however they reach you, and every job or sale from first message to done."),
    ("Booking", "Your booking page and booking inside the chat, with no double-booking."),
    ("Quotes signed on the phone", "Itemised, with your tax, signed electronically, kept with the customer."),
    ("Payment links", "Paid into your own Stripe, Mercado Pago, PayMongo, M-Pesa or bank account. We never hold the money."),
    ("Reminders and campaigns", "Appointment reminders, thank-yous and offers to the people who agreed to hear from you."),
    ("Google reviews", "The same polite review request to every customer, by hand or after each appointment."),
    ("Your website", "A page with your services, prices, hours and a Book button, made in minutes."),
    ("AI, only if you want it", "Everything works without AI. Add your own key (Google Gemini has a free tier) and it answers from your real prices and hours."),
]
TOOLS_ES = [
    ("Una sola bandeja", "WhatsApp, SMS, correo, Telegram y cualquier sistema por webhook, en una lista. Messenger e Instagram en cuanto Meta apruebe nuestra app."),
    ("Llamada perdida, mensaje de vuelta", "Si no puedes contestar, quien llamó recibe un mensaje de tu número en segundos."),
    ("Clientes y embudo", "Una ficha por persona, llegue por donde llegue, y cada venta o trabajo del primer mensaje al cierre."),
    ("Agenda", "Tu página de reservas y reservas dentro del chat, sin citas encimadas."),
    ("Cotizaciones firmadas en el celular", "Con el IVA, firma electrónica y guardadas con el cliente."),
    ("Ligas de pago", "A tu propia cuenta: Mercado Pago, Stripe o transferencia. Nunca tocamos tu dinero."),
    ("Recordatorios y campañas", "Recordatorios de citas, agradecimientos y promociones a quien aceptó recibirlas."),
    ("Reseñas de Google", "La misma petición amable a cada cliente, a mano o después de cada cita."),
    ("Tu sitio web", "Una página con tus servicios, precios, horario y botón para reservar, lista en minutos."),
    ("IA, solo si la quieres", "Todo funciona sin IA. Agrega tu propia clave (Gemini de Google es gratis) y contesta con tus precios y horarios reales."),
]

# ---------------------------------------------------------------- US trades: slug -> (name, customer word, 3 moments, 1 FAQ)
US = {
    "hvac": ("HVAC companies", "customers",
             [("A no-heat call while you're on a roof", "Missed-call text-back answers in seconds from your business number, so the job doesn't go to the next name on Google."),
              ("Estimates that wait", "Send the estimate from your phone; the customer signs it there and it stays with their history."),
              ("Seasonal tune-ups", "A text or email campaign to the customers who said yes, before the first cold week.")],
             ("Does it work for a crew of techs?", "Yes. Invite the whole team — one flat price, no per-user fees — and keep settings with the owners.")),
    "plumbers": ("plumbers", "customers",
                 [("A burst pipe at 7 am", "The caller gets a text right away while you finish the job in front of you."),
                  ("Quotes for water heaters and re-pipes", "Itemised quotes signed on the phone, kept with the customer's history."),
                  ("Reviews after every job", "A polite Google review request two hours after the appointment.")],
                 ("Can my office manager answer texts too?", "Yes. Texts land in one shared inbox; everyone sees what was answered.")),
    "electricians": ("electricians", "customers",
                     [("Calls you can't take on a panel job", "Missed calls get an automatic text from your business number."),
                      ("Estimates and sign-offs", "Send the estimate; the customer signs electronically and a fingerprinted copy is kept."),
                      ("Past customers who need more work", "A campaign to the people who agreed to hear from you.")],
                     ("Can customers sign estimates online?", "Yes. They sign on their phone by typing their name; who, when and the document's fingerprint are kept.")),
    "salons": ("hair salons", "clients",
               [("“Do you have a spot at 5?”", "Answer from one inbox — or let them book inside the chat — between clients."),
                ("Clients who stopped coming", "A campaign to everyone who said yes to offers."),
                ("Your services and prices online", "A website with your own booking page, made in minutes.")],
               ("Do clients need an app to book?", "No. They book on your page or by texting; you see it on your calendar.")),
    "barbershops": ("barbershops", "clients",
                    [("Walk-ins and texts at once", "Texts and missed calls are answered from one place while you cut."),
                     ("Appointment reminders", "A reminder the day before, so fewer chairs sit empty."),
                     ("More Google reviews", "Ask every client in one tap, or automatically after each appointment.")],
                    ("Is it only for big shops?", "No — one flat price for the whole shop, no per-chair fees.")),
    "dental-offices": ("dental offices", "patients",
                       [("Every patient message in one place", "Texts and email answered from one inbox, with the patient's history beside it."),
                        ("Reminders before every visit", "Automatic reminders cut no-shows."),
                        ("Reviews from happy patients", "The same polite request to everyone after a visit — the way the FTC wants it.")],
                       ("Is it a replacement for my practice software?", "No. Valeria One runs the front office — messages, reminders, reviews, website. Keep your clinical system for charts and billing.")),
    "auto-repair": ("auto repair shops", "customers",
                    [("“Is my car ready?”", "Answer from the inbox with the customer's history in view."),
                     ("Estimates and approvals", "Send the estimate; the customer signs online; it's kept with their record."),
                     ("Service reminders", "Oil changes and inspections by text, to customers who said yes.")],
                    ("Can I text from my shop's number?", "Yes — your own Twilio number, in your shop's name.")),
    "gyms": ("gyms and fitness studios", "members",
             [("Trial classes that convert", "Every lead from your website lands in one list with a follow-up."),
              ("Bookings for classes and sessions", "Members book on your page or in the chat, with no double-booking."),
              ("Offers to past members", "A text or email campaign to the members who said yes.")],
             ("Can members pay online?", "Yes. Send a payment link to your own Stripe or other provider; we never hold the money.")),
    "cleaning": ("cleaning businesses", "clients",
                 [("Quote requests from your website", "Every message lands in your client list with what they need."),
                  ("Recurring clients", "Reminders keep the regulars on your calendar."),
                  ("Reviews after each clean", "A one-tap Google review request.")],
                 ("Can my cleaners see the schedule?", "Yes. Invite your team; staff see the inbox, customers and calendar.")),
    "landscaping": ("landscaping companies", "customers",
                    [("Calls you miss while mowing", "Missed-call text-back keeps the conversation going."),
                     ("Spring and fall jobs", "A seasonal campaign to the customers who said yes."),
                     ("Bids on the spot", "Send an itemised bid from your phone; the customer signs it there.")],
                    ("Do I need a computer?", "No. It works in your phone's browser.")),
}

# ---------------------------------------------------------------- US alternatives: slug -> (name, their price, catch, they do better, why switch)
ALT = {
    "podium": ("Podium", "$399+ a month", "Contracts reported; message credits by plan",
               ["Built-in payments processing and terminals", "Website chat and mature integrations", "A big support team"],
               ["One flat price, no contract", "CRM, booking, quotes and your website included", "Missed-call text-back on your own number"]),
    "gohighlevel": ("GoHighLevel", "$97–497 a month + usage", "Often $200–600 real; weeks to learn; built for agencies",
                    ["Funnels and a deep automation builder", "White-label for agencies", "A large community of templates"],
                    ["Made for the owner, set up in an afternoon", "One flat price, no usage add-ons from us", "Open source: run it on your own server if you like"]),
    "birdeye": ("Birdeye", "$299–449 a month per location", "Priced per location",
                ["Listings on 60+ directories", "Deep multi-location reputation tools", "Survey and insights products"],
                ["Review requests plus CRM, texting, booking and a website", "One flat price for the whole business", "No contract"]),
    "keap": ("Keap", "$299 a month (2 users)", "Extra users $39 each; paid onboarding required",
             ["Mature email marketing automation", "Invoices and payments", "Long track record"],
             ["No per-user fees", "No onboarding fee", "Texting and missed-call text-back built in"]),
    "hubspot": ("HubSpot", "$20 a seat (Starter)", "A big jump to Professional + $1,500–3,000 onboarding",
                ["A huge ecosystem and integrations", "Full marketing suite for larger teams", "Free CRM tier"],
                ["Flat price, whole team", "Texting, reviews, booking and website included", "No onboarding fee"]),
}

# ---------------------------------------------------------------- México: slug -> (nombre, palabra cliente, 3 momentos)
MX = {
    "esteticas": ("estéticas y salones", "clientas", [("«¿Tienen lugar a las 5?»", "Contestas desde una sola bandeja, o reservan solas dentro del chat."),
                  ("Clientas que ya no regresaron", "Una campaña a quien aceptó recibir promociones."),
                  ("Tus servicios y precios en línea", "Un sitio con tu página de reservas, listo en minutos.")]),
    "barberias": ("barberías", "clientes", [("Mensajes mientras cortas", "WhatsApp, SMS y correo en una bandeja."),
                  ("Recordatorio un día antes", "Menos sillas vacías por citas olvidadas."),
                  ("Reseñas en Google", "La misma petición amable a cada cliente después de su cita.")]),
    "consultorios": ("consultorios", "pacientes", [("Pacientes en una sola lista", "Con su historial de mensajes y citas."),
                     ("Recordatorios de cita", "Automáticos, para que no falten."),
                     ("Tu consultorio en Google", "Un sitio con horario, ubicación y botón para reservar.")]),
    "dentistas": ("consultorios dentales", "pacientes", [("Mensajes de pacientes ordenados", "En una bandeja, con su historial al lado."),
                  ("Recordatorios antes de cada visita", "Menos citas perdidas."),
                  ("Presupuestos firmados", "Firma electrónica en el celular, guardada con su huella digital.")]),
    "gimnasios": ("gimnasios y estudios", "socios", [("Clases de prueba que se convierten", "Cada interesado de tu sitio llega a tu lista con seguimiento."),
                  ("Reservas de clases", "Desde tu página o dentro del chat, sin encimarse."),
                  ("Promociones del mes", "A quien aceptó recibirlas.")]),
    "talleres": ("talleres mecánicos", "clientes", [("«¿Ya está mi carro?»", "Contestas con el historial del cliente a la vista."),
                 ("Cotizaciones firmadas", "Mandas, firman en línea, queda guardada."),
                 ("Servicios de temporada", "Una campaña a tus clientes que aceptaron.")]),
    "servicios-hogar": ("servicios para el hogar", "clientes", [("Llamadas que no alcanzas a tomar", "Un mensaje de vuelta en segundos desde tu número."),
                        ("Cotizaciones en el momento", "Desde tu celular; el cliente firma ahí mismo."),
                        ("Cobros sin perseguir", "Liga de pago a tu propia cuenta o transferencia.")]),
}


def pages() -> list[str]:
    return (["/", "/mx", "/pricing", "/missed-call-text-back", "/mx/crm-con-whatsapp", "/terms", "/privacy", "/texting-terms",
             "/mx/terminos", "/mx/privacidad"]
            + [f"/crm-for/{s}" for s in US] + [f"/alternatives/{s}" for s in ALT] + [f"/mx/crm-para/{s}" for s in MX]
            + notes.pages())


def free_words() -> dict:
    """What a new business gets free, as the public pages say it: the launch months while that window is open."""
    from ..services import billing as BL
    m = BL.launch_months() if BL.enabled() else 0
    return {"free_en": f"{m} months free", "free_es": f"{m} meses gratis"} if m else {"free_en": "30 days free", "free_es": "30 días gratis"}


def _render(request: Request, **kw) -> HTMLResponse:
    from . import render
    return render(request, "marketing.html", None, usd=price_usd(), mxn=price_mxn(), demo=DEMO, us_inds=US, alts=ALT, base=config.PUBLIC_URL(),
                  mx_inds=MX, tools_en=TOOLS_EN, tools_es=TOOLS_ES, **free_words(), **kw)


def home(request: Request) -> HTMLResponse:
    return _render(request, kind="home", es=False, slug="")


@router.get("/mx")
async def mx_home(request: Request):
    if not on():
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="mx_home", es=True, slug="mx")


@router.get("/missed-call-text-back")
async def missed_call(request: Request):
    if not on():
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="mctb", es=False, slug="missed-call-text-back")


@router.get("/crm-for/{slug}")
async def crm_for(request: Request, slug: str):
    if not on() or slug not in US:
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="industry", es=False, slug=slug, d=US[slug])


@router.get("/alternatives/{slug}")
async def alternative(request: Request, slug: str):
    if not on() or slug not in ALT:
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="alt", es=False, slug=slug, d=ALT[slug])


@router.get("/mx/crm-con-whatsapp")
async def mx_whatsapp(request: Request):
    if not on():
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="mx_wa", es=True, slug="crm-con-whatsapp")


@router.get("/mx/crm-para/{slug}")
async def crm_para(request: Request, slug: str):
    if not on() or slug not in MX:
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="mx_ind", es=True, slug=slug, d=MX[slug])


@router.get("/sitemap.xml")
async def sitemap():
    if not on():
        return Response("Not found", status_code=404)
    from xml.sax.saxutils import escape
    base = config.PUBLIC_URL()
    body = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{escape(base + p)}</loc></url>" for p in pages()) + "</urlset>")
    return Response(body, media_type="application/xml")


@router.get("/robots.txt")
async def robots():
    if not on():
        return PlainTextResponse("User-agent: *\nDisallow: /app\n")
    return PlainTextResponse(aisearch.robots_txt())


@router.get("/start")
async def start_alias():
    return RedirectResponse("/signup", status_code=301)


# ---------------------------------------------------------------- Terms, Privacy, Texting (drafts, pending legal review)
_PRIV_EN, _PRIV_ES = '<a href="/privacy">Privacy Policy</a>', '<a href="/mx/privacidad">Aviso de privacidad</a>'
LEGAL = {
    "terms": ("Terms of Service", False, [
        ("1. What Valeria One is", "Software for running a small business: an inbox for your channels, customers and a pipeline, booking, quotes with e-signature, payment links, follow-ups, review requests, a website, team tools and optional AI. You connect your own accounts (your email, your Twilio number, your AI key, your payment processor). We don't resell or mark up those services."),
        ("2. Your account", "You must be at least 18 and authorized to act for your business. You're responsible for what happens under your account and for keeping your password private. Tell us right away if you think someone else got in."),
        ("3. Your data", f"Your customer data belongs to you. We process it only to run Valeria One for you, as described in our {_PRIV_EN}. You can get all of it at any time through your API key, including on the free plan."),
        ("4. Your customers and your messages", 'You are responsible for having permission to contact the people in your account, and for following the laws that apply to your messages — including CAN-SPAM for email and the TCPA and carrier rules for texts (see our <a href="/texting-terms">Texting terms</a>). You may not use Valeria One to send spam, to harass anyone, or for anything illegal.'),
        ("5. Price and payment", 'New businesses get a free period: 30 days, or the months of a launch offer when sign-up shows one. After that, Valeria One costs one flat monthly price shown on our <a href="/pricing">pricing page</a>, plus applicable taxes, for your whole team. There are no contracts and no setup fees. We never charge you without your OK; if you stop paying, your business moves to the free plan and nothing is deleted. Services you connect (your carrier, your AI provider, your payment processor) bill you directly under their own terms.'),
        ("6. Availability and changes", "We work to keep Valeria One available and your data backed up every night, but we can't promise it will never be interrupted. We may improve or change features; if a change takes away something important you pay for, we'll tell you ahead of time."),
        ("7. Liability", "Valeria One is provided “as is”. To the extent the law allows, we are not liable for indirect or consequential losses, and our total liability is limited to what you paid us in the 12 months before the claim."),
        ("8. Ending", "You can stop using Valeria One anytime. We may pause accounts that break these terms (for example, spam). Either way, you can get your data."),
        ("9. Open source", 'Valeria One is open source under the AGPL-3.0 (<a href="https://github.com/valeriaone/valeria-one">code</a>). “Valeria One” is a trademark of Tadewan Pascua.')]),
    "privacy": ("Privacy Policy", False, [
        ("What we collect", "About you (the business): your name, email, business name, country, type of business and time zone, and how you use Valeria One. About your customers: whatever you, your channels or your website put in your account — names, contact details, notes, appointments, quotes, payments and messages."),
        ("How we use it", "Only to provide Valeria One to you: to run your account, send the messages you ask us to send, keep records you need (such as e-signature records), secure the service and help you when you ask. We don't sell personal information and we don't use your customer data for advertising."),
        ("Who processes it", "Valeria One runs on our own servers. When you connect your own accounts (email, Twilio, AI, payments, Google), the data needed for that connection goes to those providers under their terms, on your behalf. For your customers' data, you are the business in charge and we act as your service provider."),
        ("Your choices and rights", "You can see, correct and delete your data in your account, get it through the API, or ask us. Depending on where you live (for example California, Colorado, Virginia, the EU or Mexico) you may have more rights to know, correct, delete or object; we honor those requests. Your customers can ask you, or us, to stop contacting them, and replying STOP stops texts."),
        ("Security and retention", "Data travels encrypted, the secrets you connect (tokens, passwords, keys) are encrypted at rest, and we back up every night. We keep your data while your account is open and for a limited time after, unless the law requires longer (for example, signed-document records)."),
        ("Cookies", "Only the cookie needed to keep you signed in and secure. No tracking pixels.")]),
    "texting-terms": ("Texting terms", False, [
        ("For businesses using Valeria One", "Texts are sent from your own number, on your own Twilio account. Before texting anyone you must have their consent — written consent for marketing messages — and your number must be registered with the carriers (A2P 10DLC or toll-free verification) in your Twilio account. Marketing messages are only sent at reasonable local hours. Missed-call text-back sends one text to a caller you couldn't answer, at most once every 30 minutes."),
        ("For people receiving texts", "Message frequency varies. Message and data rates may apply. Reply STOP to stop at any time; it is honored right away. Carriers are not liable for delayed or undelivered messages."),
        ("Privacy", f"Phone numbers and consent are never sold or shared with third parties for their marketing. See the {_PRIV_EN}.")]),
    "mx/terminos": ("Términos del servicio", True, [
        ("1. Qué es Valeria One", "Un sistema para llevar un negocio: bandeja de tus canales, clientes y embudo, agenda, cotizaciones con firma electrónica, ligas de pago, seguimientos, reseñas, sitio web, equipo e IA opcional. Conectas tus propias cuentas (tu correo, tu número, tu clave de IA, tu cuenta de pagos). No revendemos ni encarecemos esos servicios."),
        ("2. Tu cuenta", "Debes ser mayor de edad y estar autorizado para actuar por tu negocio. Eres responsable de lo que pase en tu cuenta y de cuidar tu contraseña. Avísanos de inmediato si crees que alguien más entró."),
        ("3. Tus datos", f"Los datos de tus clientes son tuyos. Los tratamos solo para darte Valeria One, como dice nuestro {_PRIV_ES}. Puedes obtenerlos todos en cualquier momento con tu clave de API, también en el plan gratis."),
        ("4. Tus clientes y tus mensajes", "Eres responsable de tener permiso para contactar a las personas de tu cuenta y de cumplir las leyes que aplican a tus mensajes, incluida la protección de datos personales. No puedes usar Valeria One para spam, acoso ni nada ilegal."),
        ("5. Precio y pago", 'Los negocios nuevos tienen un periodo gratis: 30 días, o los meses de una oferta de lanzamiento cuando el registro la muestra. Después, Valeria One cuesta un precio mensual fijo que ves en la <a href="/pricing">página de precios</a>, más IVA, por todo tu equipo. Sin contrato y sin costo de instalación. Nunca te cobramos sin tu autorización; si dejas de pagar, tu negocio pasa al plan gratis y no se borra nada. Los servicios que conectes te cobran directamente con sus propios términos.'),
        ("6. Disponibilidad y cambios", "Trabajamos para que Valeria One esté disponible y tus datos respaldados cada noche, pero no podemos prometer que nunca se interrumpa. Podemos mejorar o cambiar funciones; si un cambio quita algo importante que pagas, te avisamos con anticipación."),
        ("7. Responsabilidad", "Valeria One se ofrece “tal cual”. En la medida que la ley lo permita, no respondemos por daños indirectos, y nuestra responsabilidad total se limita a lo que nos pagaste en los 12 meses anteriores al reclamo."),
        ("8. Terminación", "Puedes dejar de usar Valeria One cuando quieras. Podemos pausar cuentas que incumplan estos términos (por ejemplo, spam). En ambos casos puedes obtener tus datos."),
        ("9. Código abierto", 'Valeria One es código abierto bajo AGPL-3.0 (<a href="https://github.com/valeriaone/valeria-one">código</a>). «Valeria One» es una marca de Tadewan Pascua.')]),
    "mx/privacidad": ("Aviso de privacidad", True, [
        ("Responsable", "Tadewan Pascua, creador de Valeria One, es responsable del tratamiento de los datos de las cuentas de negocio. Contacto: tad.pascua@valai.mx."),
        ("Qué datos tratamos", "Del negocio: nombre, correo, nombre del negocio, país, giro y zona horaria, y cómo usas Valeria One. De tus clientes: lo que tú, tus canales o tu sitio pongan en tu cuenta (nombres, datos de contacto, notas, citas, cotizaciones, pagos y mensajes). Respecto de los datos de tus clientes, tú eres el responsable y nosotros el encargado."),
        ("Para qué", "Solo para darte Valeria One: operar tu cuenta, enviar los mensajes que nos pides, guardar constancias que necesitas (como las de firma electrónica), proteger el servicio y ayudarte cuando lo pidas. No vendemos datos personales ni usamos los datos de tus clientes para publicidad."),
        ("Transferencias", "Cuando conectas tus propias cuentas (correo, Twilio, IA, pagos, Google), los datos necesarios para esa conexión van a esos proveedores, a tu nombre y con sus términos."),
        ("Derechos ARCO", "Puedes acceder, rectificar, cancelar u oponerte al tratamiento de tus datos desde tu cuenta o escribiendo a tad.pascua@valai.mx. Tus clientes pueden pedirte, o pedirnos, que dejen de contactarlos; responder BAJA o STOP detiene los mensajes."),
        ("Seguridad y conservación", "Los datos viajan cifrados, las credenciales que conectas se guardan cifradas y respaldamos cada noche. Conservamos los datos mientras tu cuenta esté abierta y un tiempo limitado después, salvo que la ley pida más (por ejemplo, constancias de documentos firmados)."),
        ("Cookies", "Solo la necesaria para mantener tu sesión segura. Sin píxeles de rastreo.")]),
}


def _legal(request: Request, key: str):
    from . import render
    title, es, sections = LEGAL[key]
    return render(request, "legal.html", None, doc_title=title, es=es, sections=sections)


@router.get("/terms")
async def terms(request: Request):
    return _legal(request, "terms") if on() else HTMLResponse("Not found", status_code=404)


@router.get("/privacy")
async def privacy(request: Request):
    return _legal(request, "privacy") if on() else HTMLResponse("Not found", status_code=404)


@router.get("/texting-terms")
async def texting_terms(request: Request):
    return _legal(request, "texting-terms") if on() else HTMLResponse("Not found", status_code=404)


@router.get("/mx/terminos")
async def mx_terms(request: Request):
    return _legal(request, "mx/terminos") if on() else HTMLResponse("Not found", status_code=404)


@router.get("/mx/privacidad")
async def mx_privacy(request: Request):
    return _legal(request, "mx/privacidad") if on() else HTMLResponse("Not found", status_code=404)
