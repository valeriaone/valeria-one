"""Field Notes, the free check-up and Our promise — the pages that help a server that sells Valeria One get found.

Field Notes (/notes in English, /mx/notas in Spanish) are short, practical articles for any small business: what
happens to missed calls, how fast to reply, how to ask for reviews, the rules for texting and WhatsApp. Every
claim about Valeria One in them is something it does today; nothing invents a statistic.

The check-up (/score, /mx/diagnostico) is eight questions, scored on the server, nothing stored and no email
asked: a score out of 100, the three biggest gaps, and — from the owner's own numbers — how many calls a month
reach nobody.

Our promise (/promise, /mx/promesa) is how we describe and offer Valeria One, written down so anyone can hold us to it.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from .. import config

router = APIRouter(include_in_schema=False)
UPDATED = "2026-10-25"


# ---------------------------------------------------------------- Field Notes
def _n(slug, lang, pair, title, desc, minutes, tag, takeaways, sections, faq, tool):
    return {"slug": slug, "lang": lang, "pair": pair, "title": title, "desc": desc, "minutes": minutes, "tag": tag,
            "date": UPDATED, "takeaways": takeaways, "sections": sections, "faq": faq, "tool": tool}


NOTES = [
    _n("missed-calls", "en", None,
       "What happens to the calls you don't answer",
       "A missed call is often a customer calling the next business on the list. Here is how one text from your own number keeps them.",
       5, "Calls",
       ["Many callers who reach voicemail hang up without a message and try the next name on the list.",
        "One short text from your business number gives them something to answer while you finish the job in front of you.",
        "Ring your phone first and text only if nobody picks up; never text the same caller twice in a few minutes.",
        "A text back to someone who just called you is a reply, not a promotion. Keep it that way."],
       [("Why the missed call is the one that costs you",
         ["Someone who calls a business usually needs something now. They searched, they chose you, they dialed. If nobody answers, the easiest next step for them is the second result, not your voicemail greeting.",
          "A voicemail asks them to wait. A text asks them a question they can answer from wherever they are, and it keeps your business as the open conversation on their phone."]),
        ("What a good text back says",
         ["Short, with your business name, an apology and one easy next step. For example: “Hi, this is Green Valley Plumbing. Sorry we missed your call, we're with a customer. Text us what you need and we'll get right back to you.”",
          "Keep it under 160 characters so it arrives as one message, and write it the way you talk."]),
        ("Ring first, then text",
         ["If you can often pick up, let the call ring your cell for about 20 seconds first. If you answer, no text goes out. If nobody does, or you're busy, the caller gets the text.",
          "Callers who try again right away shouldn't get a second text: one text per caller every 30 minutes is plenty. Hidden or blocked numbers can't be texted, so they hear a short message asking them to call again."]),
        ("Keep it a reply, not a campaign",
         ["The text answers a call the person just made. Don't add them to promotions unless they say yes to that separately. In the US, business texting goes out from a number registered with the carriers (A2P 10DLC or toll-free verification), and anyone can reply STOP."]),
        ("How Valeria One does it",
         ["Connect your own Twilio number once and Valeria One points its calls and texts at your account. Add your cell if you want it to ring first, and change the message if you like. Every text back lands in your inbox with the caller's record, so you can book them, quote them or send a payment link from the same place."])],
       [("Does it work with the number I already have?", "If your number is in Twilio, yes. If it's with another carrier, you can move it to Twilio or forward unanswered calls to a Twilio number; the text then comes from that number."),
        ("What if the caller is on a landline?", "A landline can't receive the text. That's one more reason to let your phone ring first.")],
       ("Missed-call text-back", "/missed-call-text-back")),

    _n("reply-first", "en", "responder-rapido",
       "Reply first: why speed beats the perfect answer",
       "People who ask for a price often ask several businesses at once. The one that answers first usually gets the conversation.",
       4, "Inbox",
       ["When people ask for a price, they often ask two or three businesses at the same time.",
        "A quick reply that asks one good question beats a complete answer three hours later.",
        "Put every channel in one inbox so nobody has to check five apps to know who is waiting.",
        "For the hours you can't type, an assistant that answers from your real prices and hours keeps the door open."],
       [("Why speed matters more than polish",
         ["Someone comparing businesses is going to talk to whoever answers. Once a conversation has started, they tend to keep it going there rather than start over somewhere else.",
          "That means the first reply doesn't have to be complete. It has to be fast and move things forward."]),
        ("A fast reply can be short",
         ["“Thanks for writing! What's the address, and is this week OK?” is a good first reply. It shows a person is there, and it asks the question you need answered before you can quote.",
          "Save the long explanation for when you know what they need."]),
        ("Make it something the whole team can do",
         ["If messages arrive on a personal WhatsApp, a work email and a texting app, replies depend on who happens to look. One shared inbox means anyone on the team can answer, and everyone can see what was already said."]),
        ("After hours",
         ["Decide what happens at night. At minimum, people should know when you'll answer. If you want more, an AI assistant can answer common questions from your own prices and hours and leave the conversation for you in the morning."]),
        ("How Valeria One does it",
         ["WhatsApp, texts, email, Telegram and anything that can send a webhook arrive in one inbox, with one record per person however they reach you. The whole team works from it. AI replies are optional and use your own key; Google Gemini has a free tier."])],
       [("Do I need AI to reply fast?", "No. Most of the speed comes from having every message in one place on your phone. AI only helps with the hours you aren't there.")],
       ("One inbox", "/")),

    _n("google-reviews", "en", "resenas-google",
       "How to ask every customer for a Google review",
       "The right way to ask for reviews: everyone, soon after the job, with a direct link, and nothing in exchange.",
       5, "Reviews",
       ["Ask every customer the same way. Google's rules forbid asking only the happy ones, and consumer regulators (in the US, the FTC) warn against it.",
        "Ask soon after the job, with a direct link to your review page.",
        "Never pay, discount or reward anyone for a review.",
        "Ask each person once in a while, not after every visit."],
       [("Get your review link",
         ["In your Google Business Profile, choose “Ask for reviews” and copy the link. It opens the review box for your business directly, so the customer doesn't have to search for you."]),
        ("When to ask",
         ["A few hours after the job or appointment is a good time: the experience is fresh and they're no longer busy with you. Asking weeks later rarely works."]),
        ("What to say",
         ["Keep it personal and short: “Hi Ana, thank you for choosing us today. If you have a minute, a review helps a small business like ours a lot: [link]”."]),
        ("What not to do",
         ["Don't screen customers first and send only the happy ones to Google (sometimes called review gating). Google's policy prohibits selectively asking for positive reviews, and consumer regulators (in the US, the FTC's guidance for businesses) say not to do it.",
          "Don't offer discounts, gifts or entries in a draw for reviews. Google doesn't allow incentives, and many countries' consumer laws forbid them too (in the US, the FTC's 2024 rule bans rewards tied to positive reviews). And never write reviews for yourself or ask staff and family to."]),
        ("Answer what you get",
         ["Reply to reviews, especially the critical ones, calmly and briefly. Future customers read the replies too."]),
        ("How Valeria One does it",
         ["Save your review link once. Then ask from a customer's page with one tap, or let it happen automatically two hours after each appointment. Everyone gets the same polite message, each person at most once every 90 days, and the link is counted so you see who clicked. Because a review request is marketing, it only goes to people who agreed to hear from you."])],
       [("Can I ask only customers I think were happy?", "No. Ask everyone the same way; that is both the rule and the honest thing to do."),
        ("Is it OK to text the request?", "Yes, to customers who agreed to receive messages from you, at reasonable hours.")],
       ("Google reviews", "/")),

    _n("texting-rules", "en", None,
       "Texting customers in the US: consent, quiet hours and STOP",
       "The plain-English basics of business texting in the US: replies versus promotions, written permission, registered numbers, hours and opt-outs. Elsewhere the same habits are a good start; check your own country's rules.",
       6, "Rules",
       ["Replying to someone who texted or called you is different from sending promotions; promotions need their written permission.",
        "Business texting goes out from a number registered with the carriers: A2P 10DLC for regular numbers, or toll-free verification.",
        "Send marketing texts only between 8 am and 9 pm in the customer's time zone; some states, such as Florida, are stricter.",
        "STOP must stop everything, right away."],
       [("Two kinds of messages",
         ["Service messages answer something the person started: a reply to their question, a reminder for an appointment they booked, a receipt. Marketing messages promote: offers, “we miss you”, seasonal campaigns.",
          "The rules for marketing are much stricter, so it helps to label every message as one or the other before it goes out."]),
        ("Getting permission the right way",
         ["Ask with a checkbox that isn't ticked in advance, say what they'll receive and how often, and keep a record of when and how they agreed.",
          "Keep two separate permissions: one to reply to their request, and one to send promotions. Agreeing to be contacted about a quote is not agreeing to receive offers."]),
        ("Register your number",
         ["US carriers expect business texts to come from a registered number. With Twilio this is A2P 10DLC registration for local numbers, or toll-free verification. Unregistered traffic gets filtered or blocked."]),
        ("Hours and opt-outs",
         ["Send promotions at reasonable local hours, between 8 am and 9 pm at the latest, and check your state's rules, since some are stricter. Make STOP work instantly and permanently, and don't keep texting someone who asked you to stop.",
          "This is general information, not legal advice. If texting is a big part of your business, talk to a lawyer about your exact situation."]),
        ("How Valeria One does it",
         ["Every message is either service or marketing. Each customer's marketing permission is stored with where it came from. Marketing to someone without permission is blocked, US marketing doesn't go out between 9 pm and 8 am local time, and STOP (and words like UNSUBSCRIBE) is honored right away. Texts go from your own Twilio number, registered in your own Twilio account."])],
       [("Do appointment reminders need written consent?", "Reminders for an appointment the customer booked are service messages, but they still need a number the customer gave you and a way to say STOP. Promotions need written permission."),
        ("Can I buy a list and text it?", "No. Text only people who gave you their number and, for promotions, their permission.")],
       ("Texting terms", "/texting-terms")),

    _n("one-inbox", "en", "una-sola-bandeja",
       "Five apps, one customer: why everything should land in one inbox",
       "Customers choose how to reach you. When messages are scattered across apps, some go unanswered. One inbox fixes that.",
       4, "Inbox",
       ["Customers write on whatever they use: WhatsApp, texts, email, social media.",
        "When messages are scattered across apps, the one nobody checked is the one that goes cold.",
        "One record per person, whatever channel they use, keeps the whole story together.",
        "A shared inbox lets the team answer without stepping on each other."],
       [("The problem",
         ["A customer texts on Monday, emails a photo on Tuesday and sends a WhatsApp on Wednesday. If those live in three apps on two phones, nobody has the full picture, and something gets missed."]),
        ("One person, one record",
         ["The fix is simple to describe: every message, from every channel, lands in one list, attached to the person who sent it. Their history, appointments, quotes and payments sit next to the conversation."]),
        ("What to connect first",
         ["Start with where most of your customers already write. For many businesses that's WhatsApp or texts, then email. Add the rest as you go."]),
        ("How Valeria One does it",
         ["WhatsApp, SMS, email, Telegram and any system that can send a webhook come into one inbox. Messenger and Instagram join as soon as Meta approves our app. Your whole team works from it."])],
       [("Do I lose my WhatsApp history?", "No. Old chats stay where they are; new conversations arrive in the inbox once you connect.")],
       ("One inbox", "/")),

    _n("follow-up", "en", "seguimiento",
       "The follow-up nobody sends",
       "Reminders, thank-yous and a check-in after the job: the small messages most businesses mean to send and don't.",
       4, "Follow-ups",
       ["The messages that win repeat business are small: a reminder, a thank-you, a check-in.",
        "They don't get sent because nobody has time to remember them.",
        "Set them up once as rules and they go out on their own.",
        "Promotions only go to people who said yes to them."],
       [("The four messages worth automating",
         ["A reminder before an appointment, so fewer people forget. A thank-you after the job. A welcome when someone new gets in touch. A thank-you when a sale closes.",
          "None of these needs to be clever. They need to arrive on time, every time."]),
        ("Rules, not memory",
         ["Write each one once, decide when it goes (for example, the day before an appointment, or two hours after it), and let it run. Check now and then that the wording still sounds like you."]),
        ("Campaigns, carefully",
         ["Seasonal offers and “we miss you” messages are marketing. Send them only to people who agreed, at reasonable hours, and make it easy to say no."]),
        ("How Valeria One does it",
         ["Follow-ups run on four triggers: before an appointment, after an appointment, when a new customer arrives, and when a sale is won. Campaigns go to a tag of customers who agreed to marketing. Every message follows the country's rules on consent and hours."])],
       [("Can follow-ups go by WhatsApp?", "Yes, by whatever channel you use with that customer. On WhatsApp, messages outside the 24-hour window need an approved template.")],
       ("Follow-ups", "/")),

    # ------------------------------------------------------------ Spanish (Mexico)
    _n("whatsapp-para-negocios", "es", None,
       "WhatsApp para negocios en México: la app, la API y la regla de las 24 horas",
       "Qué cambia entre WhatsApp Business y la API, por qué existe la ventana de 24 horas y qué necesitas para escribirle primero a un cliente.",
       6, "WhatsApp",
       ["WhatsApp Business (la app) sirve para empezar; para un equipo y automatizaciones se usa la API de WhatsApp.",
        "Cuando un cliente te escribe, tienes 24 horas para contestarle con mensajes normales.",
        "Para escribirle primero, o fuera de esas 24 horas, Meta pide plantillas aprobadas y las cobra.",
        "Antes de mandar promociones necesitas que el cliente haya aceptado recibirlas."],
       [("La app y la API",
         ["La app WhatsApp Business es gratis y se usa desde el celular. Para un negocio pequeño es un buen inicio, pero se complica cuando varias personas contestan o quieres que los mensajes lleguen a un sistema.",
          "La API de WhatsApp (la plataforma de Meta para negocios) se usa a través de un proveedor oficial, como Twilio, o directo con Meta. Permite que todo el equipo conteste desde una bandeja y que los recordatorios salgan solos."]),
        ("La ventana de 24 horas",
         ["Cuando un cliente te escribe, se abre una ventana de 24 horas para contestarle libremente. Contestar a tiempo, además de ser buen servicio, es lo más barato: esas respuestas no llevan plantilla."]),
        ("Escribir primero: plantillas",
         ["Si tú inicias la conversación (un recordatorio, un aviso, una promoción) o ya pasaron las 24 horas, el mensaje tiene que ser una plantilla aprobada por Meta, y Meta la cobra según su tipo. Por eso conviene contestar rápido y dejar las plantillas para lo necesario."]),
        ("Permiso del cliente",
         ["Las reglas de WhatsApp piden que el cliente haya aceptado recibir tus mensajes antes de escribirle primero. Para promociones, pide ese permiso por separado y guarda cuándo y cómo lo dio. Quien escriba BAJA debe dejar de recibir mensajes."]),
        ("Cómo lo hace Valeria One",
         ["Conectas tu propio número por Twilio y tus conversaciones de WhatsApp llegan a la misma bandeja que SMS y correo, con la ficha de cada cliente. Valeria One respeta la ventana de 24 horas, no manda promociones a quien no aceptó y atiende BAJA y STOP de inmediato. La conexión directa con Meta llega en cuanto aprueben nuestra app."])],
       [("¿Mis clientes ven el nombre de mi negocio?", "Sí: les escribes desde tu propio número."),
        ("¿Cuánto cobra Meta?", "Contestar dentro de las 24 horas no lleva plantilla. Las plantillas que tú inicias tienen precio según su tipo; consulta la tabla vigente de Meta.")],
       ("CRM con WhatsApp", "/mx/crm-con-whatsapp")),

    _n("responder-rapido", "es", "reply-first",
       "Contesta primero: por qué la rapidez le gana a la respuesta perfecta",
       "Quien pide un precio suele preguntar en dos o tres negocios a la vez. El que contesta primero se queda con la conversación.",
       4, "Bandeja",
       ["Quien pide un precio muchas veces pregunta en varios negocios al mismo tiempo.",
        "Una respuesta rápida con una buena pregunta le gana a una respuesta completa tres horas después.",
        "Con todo en una sola bandeja nadie tiene que revisar cinco apps para saber quién espera.",
        "Para las horas en que no puedes escribir, una asistente que contesta con tus precios y horarios reales mantiene la puerta abierta."],
       [("Por qué la rapidez importa más",
         ["Quien está comparando va a platicar con quien le conteste. Una vez que empezó la conversación, lo normal es seguirla ahí y no empezar de cero en otro lado."]),
        ("Una respuesta rápida puede ser corta",
         ["«¡Gracias por escribir! ¿En qué colonia estás y te queda esta semana?» es una buena primera respuesta: muestra que hay alguien y pregunta lo que necesitas para cotizar."]),
        ("Que lo pueda hacer todo el equipo",
         ["Si los mensajes llegan a un WhatsApp personal, a un correo y a otra app, contestar depende de quién se acuerde de revisar. Con una bandeja compartida cualquiera del equipo contesta y todos ven lo que ya se dijo."]),
        ("Fuera de horario",
         ["Decide qué pasa en la noche. Lo mínimo es que sepan cuándo les vas a contestar. Si quieres más, una asistente con IA contesta lo común con tus precios y horarios y te deja la conversación para la mañana."]),
        ("Cómo lo hace Valeria One",
         ["WhatsApp, SMS, correo, Telegram y cualquier sistema por webhook llegan a una sola bandeja, con una ficha por persona. Todo el equipo trabaja ahí. La IA es opcional y usa tu propia clave; Gemini de Google es gratis."])],
       [("¿Necesito IA para contestar rápido?", "No. Casi toda la rapidez viene de tener todos los mensajes en un solo lugar en tu celular.")],
       ("Una sola bandeja", "/mx")),

    _n("resenas-google", "es", "google-reviews",
       "Cómo pedirle una reseña de Google a cada cliente",
       "La forma correcta de pedir reseñas: a todos, poco después del servicio, con liga directa y sin dar nada a cambio.",
       5, "Reseñas",
       ["Pídela a todos de la misma forma: las reglas de Google prohíben pedirla solo a los clientes contentos.",
        "Pídela poco después del servicio, con la liga directa a tu página de reseñas.",
        "Nunca des descuentos, regalos ni premios a cambio de una reseña.",
        "A cada persona pídesela de vez en cuando, no en cada visita."],
       [("Consigue tu liga",
         ["En tu Perfil de Negocio de Google elige «Pedir reseñas» y copia la liga. Abre directo la ventana para calificar tu negocio, sin que el cliente tenga que buscarte."]),
        ("Cuándo pedirla",
         ["Unas horas después del servicio o la cita: la experiencia está fresca y ya no están ocupados contigo. Semanas después casi nunca funciona."]),
        ("Qué decir",
         ["Corto y personal: «Hola Ana, gracias por elegirnos hoy. Si tienes un minuto, una reseña nos ayuda mucho: [liga]»."]),
        ("Qué no hacer",
         ["No filtres primero y mandes a Google solo a los contentos: Google prohíbe pedir reseñas positivas de forma selectiva. No des incentivos y nunca escribas reseñas propias ni pidas a tu equipo o familia que lo hagan."]),
        ("Contesta las que recibas",
         ["Responde las reseñas, sobre todo las críticas, con calma y en pocas palabras. Los clientes que vienen también leen tus respuestas."]),
        ("Cómo lo hace Valeria One",
         ["Guardas tu liga una vez. Pides la reseña con un toque desde la ficha del cliente, o sale sola dos horas después de cada cita. Todos reciben el mismo mensaje amable, cada persona como máximo una vez cada 90 días, y la liga se cuenta para que veas quién le dio clic. Como es un mensaje de promoción, solo va a quien aceptó recibir tus mensajes."])],
       [("¿Puedo pedírsela solo a los que creo que quedaron contentos?", "No. A todos igual: es la regla y es lo honesto.")],
       ("Reseñas de Google", "/mx")),

    _n("aviso-de-privacidad-y-promociones", "es", None,
       "Promociones por WhatsApp y SMS en México: permiso, aviso de privacidad y BAJA",
       "Lo básico para mandar promociones sin problemas: aviso de privacidad, permiso del cliente, el REPEP de Profeco y la baja inmediata.",
       5, "Reglas",
       ["Contestarle a quien te escribió es distinto a mandarle promociones; para promociones necesitas su permiso.",
        "Si guardas datos de clientes necesitas un aviso de privacidad que diga para qué los usas.",
        "Quien está inscrito en el REPEP de Profeco no quiere publicidad: respétalo.",
        "BAJA debe detener los mensajes de inmediato."],
       [("Dos tipos de mensajes",
         ["Los mensajes de servicio responden a algo que el cliente empezó: su pregunta, el recordatorio de la cita que agendó, su recibo. Los de promoción venden: ofertas, «te extrañamos», temporadas. Conviene marcar cada mensaje como uno u otro antes de enviarlo."]),
        ("Aviso de privacidad",
         ["La ley de protección de datos personales te pide informar a tus clientes qué datos guardas, para qué y cómo pueden ejercer sus derechos ARCO (acceso, rectificación, cancelación y oposición). Ponlo donde piden sus datos: tu sitio, tu formulario, tu WhatsApp."]),
        ("Permiso para promociones",
         ["Pide el permiso aparte, con una casilla sin marcar o un «sí» por escrito, y guarda cuándo y cómo lo dio. Aceptar que le mandes una cotización no es aceptar promociones."]),
        ("REPEP y BAJA",
         ["El Registro Público para Evitar Publicidad (REPEP) de Profeco es la lista de quienes no quieren recibir publicidad. Si mandas promociones a números que no te dieron permiso, revísalo. Y quien conteste BAJA o STOP debe dejar de recibir mensajes ya.",
          "Esto es información general, no asesoría legal. Si las promociones son una parte grande de tu negocio, consúltalo con un abogado."]),
        ("Cómo lo hace Valeria One",
         ["Cada mensaje es de servicio o de promoción. El permiso de cada cliente se guarda con su origen, las promociones a quien no aceptó se bloquean y BAJA, STOP y palabras parecidas se atienden de inmediato."])],
       [("¿Puedo comprar una base de datos y mandarle promociones?", "No. Escríbele solo a quien te dio su número y, para promociones, su permiso.")],
       ("Aviso de privacidad", "/mx/privacidad")),

    _n("una-sola-bandeja", "es", "one-inbox",
       "Cinco apps, un cliente: por qué todo debe llegar a una sola bandeja",
       "Tus clientes eligen cómo escribirte. Cuando los mensajes están regados en varias apps, alguno se queda sin respuesta.",
       4, "Bandeja",
       ["Los clientes escriben por donde ya están: WhatsApp, SMS, correo, redes.",
        "Con los mensajes regados, el que nadie revisó es el cliente que se enfría.",
        "Una ficha por persona, llegue por donde llegue, junta toda la historia.",
        "Una bandeja compartida deja que el equipo conteste sin pisarse."],
       [("El problema",
         ["Un cliente manda WhatsApp el lunes, un correo con fotos el martes y un SMS el miércoles. Si eso vive en tres apps y dos celulares, nadie tiene la historia completa."]),
        ("Una persona, una ficha",
         ["La solución: cada mensaje, de cada canal, llega a una sola lista, pegado a la persona que lo mandó, con sus citas, cotizaciones y pagos al lado."]),
        ("Qué conectar primero",
         ["Empieza por donde ya te escribe la mayoría. En México casi siempre es WhatsApp, luego correo. Lo demás lo vas sumando."]),
        ("Cómo lo hace Valeria One",
         ["WhatsApp, SMS, correo, Telegram y cualquier sistema por webhook llegan a una bandeja. Messenger e Instagram se suman en cuanto Meta apruebe nuestra app. Todo tu equipo trabaja ahí."])],
       [("¿Pierdo mi historial de WhatsApp?", "No. Los chats viejos se quedan donde están; las conversaciones nuevas llegan a la bandeja cuando conectas.")],
       ("Una sola bandeja", "/mx")),

    _n("seguimiento", "es", "follow-up",
       "El seguimiento que nadie manda",
       "Recordatorios, agradecimientos y un mensaje después del servicio: lo que todos quieren mandar y casi nadie manda.",
       4, "Seguimiento",
       ["Los mensajes que traen clientes de vuelta son pequeños: un recordatorio, un gracias, un «¿cómo te fue?».",
        "No se mandan porque nadie tiene tiempo de acordarse.",
        "Se configuran una vez como reglas y salen solos.",
        "Las promociones solo van a quien aceptó."],
       [("Los cuatro mensajes que vale la pena automatizar",
         ["Un recordatorio antes de la cita, para que menos gente la olvide. Un gracias después del servicio. Una bienvenida cuando alguien nuevo te escribe. Un gracias cuando se cierra una venta."]),
        ("Reglas, no memoria",
         ["Escribe cada uno una vez, decide cuándo sale (por ejemplo, un día antes de la cita o dos horas después) y déjalo correr. De vez en cuando revisa que siga sonando a ti."]),
        ("Cobrar sin perseguir",
         ["Una liga de pago en el mismo chat ahorra la vuelta de «¿a qué cuenta te transfiero?». El dinero va directo a tu propia cuenta."]),
        ("Cómo lo hace Valeria One",
         ["Los seguimientos corren con cuatro disparadores: antes de la cita, después de la cita, cliente nuevo y venta ganada. Las campañas van a una etiqueta de clientes que aceptaron promociones. Las ligas de pago van a tu propia cuenta de Mercado Pago, Stripe o transferencia; nunca tocamos tu dinero."])],
       [("¿Los seguimientos pueden ir por WhatsApp?", "Sí, por el canal que uses con ese cliente. Fuera de las 24 horas, WhatsApp pide una plantilla aprobada.")],
       ("Seguimiento", "/mx")),
]
BY = {(n["lang"], n["slug"]): n for n in NOTES}


def note_url(n: dict) -> str:
    return f"/notes/{n['slug']}" if n["lang"] == "en" else f"/mx/notas/{n['slug']}"


def _pair_url(n: dict) -> str | None:
    other = BY.get(("es" if n["lang"] == "en" else "en", n["pair"])) if n["pair"] else None
    return note_url(other) if other else None


# ---------------------------------------------------------------- the free check-up
# (key, points per answer, english question, spanish question, english answers, spanish answers, the gap: en, es, link)
QUESTIONS = [
    ("calls", [0, 8, 20], "When you can't answer the phone, what happens?", "Cuando no puedes contestar el teléfono, ¿qué pasa?",
     ["It goes to voicemail, or nothing", "Someone else picks up, sometimes", "It's always answered, or the caller gets a text"],
     ["Va al buzón, o nada", "A veces contesta alguien más", "Siempre se contesta, o le llega un mensaje"],
     ("Missed calls go nowhere", "Las llamadas perdidas se pierden", "/missed-call-text-back", "/mx/notas/responder-rapido",
      "Turn on missed-call text-back so every caller you can't answer gets a text from your number in seconds.",
      "Que cada llamada que no contestas reciba un mensaje tuyo en segundos.")),
    ("speed", [0, 8, 20], "How fast do you usually reply to a new message?", "¿Qué tan rápido contestas un mensaje nuevo?",
     ["The next day or later", "The same day", "Within the hour"],
     ["Al día siguiente o después", "El mismo día", "En menos de una hora"],
     ("Slow first replies", "Primeras respuestas lentas", "/notes/reply-first", "/mx/notas/responder-rapido",
      "Reply first, even briefly: a short question beats a perfect answer hours later.",
      "Contesta primero, aunque sea corto: una pregunta rápida le gana a la respuesta perfecta.")),
    ("inbox", [0, 5, 10], "Where do customer messages arrive?", "¿A dónde llegan los mensajes de tus clientes?",
     ["Scattered across phones and apps", "Two or three apps", "One place the whole team sees"],
     ["Regados en celulares y apps", "Dos o tres apps", "Un solo lugar que ve todo el equipo"],
     ("Messages scattered across apps", "Mensajes regados en varias apps", "/notes/one-inbox", "/mx/notas/una-sola-bandeja",
      "Bring every channel into one inbox so nothing waits unseen.",
      "Junta todos los canales en una bandeja para que nada se quede sin ver.")),
    ("quotes", [0, 5, 10], "After you send a price or a quote, do you follow up?", "Después de mandar un precio o cotización, ¿le das seguimiento?",
     ["Rarely", "Sometimes", "Always, on a schedule"],
     ["Casi nunca", "A veces", "Siempre, con fechas"],
     ("Quotes that go quiet", "Cotizaciones que se enfrían", "/notes/follow-up", "/mx/notas/seguimiento",
      "Send quotes customers can sign on their phone, and check in a few days later.",
      "Manda cotizaciones que firmen en el celular y da seguimiento a los pocos días.")),
    ("reminders", [0, 5, 10, 10], "Do customers get a reminder before an appointment?", "¿Tus clientes reciben recordatorio antes de su cita?",
     ["No", "I send them by hand", "Yes, automatically", "We don't work with appointments"],
     ["No", "Los mando a mano", "Sí, automáticos", "No trabajamos con citas"],
     ("No appointment reminders", "Sin recordatorios de citas", "/notes/follow-up", "/mx/notas/seguimiento",
      "Set a reminder the day before, once, and let it run.",
      "Configura un recordatorio un día antes, una vez, y déjalo correr.")),
    ("reviews", [0, 5, 10], "Do you ask customers for a Google review?", "¿Le pides a tus clientes una reseña de Google?",
     ["Never", "Sometimes", "Every customer, the same way"],
     ["Nunca", "A veces", "A cada cliente, igual"],
     ("Few new reviews", "Pocas reseñas nuevas", "/notes/google-reviews", "/mx/notas/resenas-google",
      "Ask every customer the same way, a couple of hours after the job, with your direct link.",
      "Pídela a cada cliente igual, unas horas después del servicio, con tu liga directa.")),
    ("list", [0, 4, 10], "Do you have a list of past customers you're allowed to contact?", "¿Tienes una lista de clientes anteriores a los que puedes escribir?",
     ["No list", "A list, but I'm not sure they agreed", "Yes, with their permission"],
     ["No tengo lista", "Tengo lista, pero no sé si aceptaron", "Sí, con su permiso"],
     ("No permission-based customer list", "Sin lista de clientes con permiso", "/notes/texting-rules", "/mx/notas/aviso-de-privacidad-y-promociones",
      "Keep every customer in one place, with a record of who agreed to hear from you.",
      "Guarda a cada cliente en un solo lugar, con registro de quién aceptó tus mensajes.")),
    ("online", [0, 5, 10], "Can customers see your prices and book online?", "¿Tus clientes pueden ver precios y agendar en línea?",
     ["No", "Partly", "Yes"],
     ["No", "En parte", "Sí"],
     ("Hard to book you online", "Difícil agendar contigo en línea", "/", "/mx",
      "Put your services, prices, hours and a Book button on one page.",
      "Pon tus servicios, precios, horario y un botón para agendar en una página.")),
]
MISS_SHARE = [0.0, 0.1, 0.25, 0.5]                     # «about how many calls do you miss?»: none, 1 in 10, 1 in 4, 1 in 2


def score(form) -> dict:
    """Answers -> {score, band, gaps (biggest first, at most 3), missed_month}. Nothing is stored."""
    total, gaps = 0, []
    for key, pts, *_rest, gap in QUESTIONS:
        try:
            i = int(form.get(key, ""))
            got = pts[i] if 0 <= i < len(pts) else 0
        except (TypeError, ValueError):
            got = 0
        total += got
        if got < max(pts):
            gaps.append((max(pts) - got, gap))
    gaps.sort(key=lambda g: -g[0])
    try:
        calls = max(0, min(int(str(form.get("n_calls") or "0").strip() or 0), 10000))
    except ValueError:
        calls = 0
    try:
        share = MISS_SHARE[int(form.get("miss", ""))]
    except (TypeError, ValueError, IndexError):
        share = None
    missed = round(calls * share * 52 / 12) if share is not None and calls else None
    band = "strong" if total >= 80 else "ok" if total >= 50 else "leaking"
    return {"score": total, "band": band, "gaps": [g for _, g in gaps[:3]], "missed_month": missed}


# ---------------------------------------------------------------- Our promise
PROMISE = {
    "en": ("Our promise", "How we describe and offer Valeria One, written down so you can hold us to it.", [
        ("One price, in the open", "One plan for your whole team, on the pricing page, with no setup fee and no contract. We never charge you without your OK."),
        ("Live means live", "If a page says a tool works, it works today. Anything still coming is labeled as coming, with what it's waiting on."),
        ("No invented numbers", "No made-up statistics, no fake testimonials, no “trusted by thousands”. When we compare prices with other tools, we say where the numbers came from and when."),
        ("We say what the big tools do better", "Every comparison page has a section on what the other tool does better. Bigger isn't always worse."),
        ("Your accounts, your data", "You connect your own email, number, payment account and AI key. Your customer data is yours: get all of it through your API key at any time, even on the free plan. If you stop paying, nothing is deleted."),
        ("Your customers' permission comes first", "Promotions only go to people who agreed, at reasonable hours, and STOP or BAJA works right away."),
        ("AI is optional", "Everything works without AI. If you want it, you use your own key and it answers from your real prices and hours."),
        ("Open source", "The code is public under the AGPL-3.0, and you can run Valeria One on your own server with one command."),
        ("No tracking", "Our pages use one cookie to keep you signed in, and no advertising pixels."),
    ]),
    "es": ("Nuestra promesa", "Cómo describimos y ofrecemos Valeria One, por escrito, para que nos lo puedas exigir.", [
        ("Un precio, a la vista", "Un plan para todo tu equipo, en la página de precios, sin costo de instalación y sin contrato. Nunca te cobramos sin tu autorización."),
        ("Si dice que funciona, funciona", "Si una página dice que una herramienta funciona, funciona hoy. Lo que todavía no está, dice «próximamente» y qué falta."),
        ("Sin números inventados", "Ni estadísticas inventadas, ni testimonios falsos, ni «miles de clientes». Cuando comparamos precios con otras herramientas, decimos de dónde salen y de qué fecha son."),
        ("Decimos en qué nos ganan los grandes", "Cada comparación tiene una parte con lo que la otra herramienta hace mejor."),
        ("Tus cuentas, tus datos", "Conectas tu propio correo, número, cuenta de pagos y clave de IA. Los datos de tus clientes son tuyos: los obtienes con tu clave de API cuando quieras, también en el plan gratis. Si dejas de pagar, no se borra nada."),
        ("Primero el permiso de tus clientes", "Las promociones solo van a quien aceptó, en horarios razonables, y BAJA o STOP funciona de inmediato."),
        ("La IA es opcional", "Todo funciona sin IA. Si la quieres, usas tu propia clave y contesta con tus precios y horarios reales."),
        ("Código abierto", "El código es público bajo AGPL-3.0 y puedes correr Valeria One en tu propio servidor con un comando."),
        ("Sin rastreo", "Nuestras páginas usan una sola cookie para tu sesión, y ningún píxel de publicidad."),
    ]),
}


# the promises that talk price or payment, as a server that doesn't charge says them (by language, by index)
FREE_PROMISE = {
    "en": {0: ("Free, in the open", "Valeria One is free: everything included, for your whole team, in any country, with no card and no contract. If we ever add optional paid extras, we'll tell you first, and we never charge you without your OK."),
           4: ("Your accounts, your data", "You connect your own email, number, payment account and AI key. Your customer data is yours: get all of it through your API key at any time, and nothing is ever deleted behind your back.")},
    "es": {0: ("Gratis, a la vista", "Valeria One es gratis: todo incluido, para todo tu equipo, en cualquier país, sin tarjeta y sin contrato. Si algún día agregamos extras opcionales de pago, te avisamos antes, y nunca te cobramos sin tu autorización."),
           4: ("Tus cuentas, tus datos", "Conectas tu propio correo, número, cuenta de pagos y clave de IA. Los datos de tus clientes son tuyos: los obtienes con tu clave de API cuando quieras, y nunca se borra nada a tus espaldas.")},
}


def promise(lang: str) -> tuple:
    """Our promise as this server says it: PROMISE, with FREE_PROMISE swapped in when it doesn't charge."""
    from .marketing import charging
    title, lede, items = PROMISE[lang]
    if not charging():
        items = [FREE_PROMISE[lang].get(i, x) for i, x in enumerate(items)]
    return title, lede, items


def pages() -> list[str]:
    return (["/notes", "/mx/notas", "/score", "/mx/diagnostico", "/promise", "/mx/promesa"]
            + [note_url(n) for n in NOTES])


# ---------------------------------------------------------------- routes
def _off():
    from .marketing import on
    return not on()


def _render(request: Request, **kw) -> HTMLResponse:
    from . import render
    from .marketing import free_words, price_mxn, price_usd
    return render(request, "mk_extra.html", None, usd=price_usd(), mxn=price_mxn(), base=config.PUBLIC_URL(),
                  demo="https://sandbox.valai.mx/#demos", notes=NOTES, note_url=note_url, **free_words(), **kw)


@router.get("/notes")
async def notes_en(request: Request):
    if _off():
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="notes", es=False, items=[n for n in NOTES if n["lang"] == "en"], alt="/mx/notas")


@router.get("/mx/notas")
async def notes_es(request: Request):
    if _off():
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="notes", es=True, items=[n for n in NOTES if n["lang"] == "es"], alt="/notes")


def _note(request: Request, lang: str, slug: str):
    n = BY.get((lang, slug))
    if _off() or not n:
        return HTMLResponse("Not found", status_code=404)
    more = [x for x in NOTES if x["lang"] == lang and x is not n][:3]
    return _render(request, kind="note", es=lang == "es", n=n, more=more, alt=_pair_url(n))


@router.get("/notes/{slug}")
async def note_en(request: Request, slug: str):
    return _note(request, "en", slug)


@router.get("/mx/notas/{slug}")
async def note_es(request: Request, slug: str):
    return _note(request, "es", slug)


async def _score(request: Request, es: bool):
    if _off():
        return HTMLResponse("Not found", status_code=404)
    res = score(await request.form()) if request.method == "POST" else None
    return _render(request, kind="score", es=es, questions=QUESTIONS, res=res, alt="/score" if es else "/mx/diagnostico")


@router.api_route("/score", methods=["GET", "POST"])
async def score_en(request: Request):
    return await _score(request, False)


@router.api_route("/mx/diagnostico", methods=["GET", "POST"])
async def score_es(request: Request):
    return await _score(request, True)


@router.get("/promise")
async def promise_en(request: Request):
    if _off():
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="promise", es=False, p=promise("en"), alt="/mx/promesa")


@router.get("/mx/promesa")
async def promise_es(request: Request):
    if _off():
        return HTMLResponse("Not found", status_code=404)
    return _render(request, kind="promise", es=True, p=promise("es"), alt="/promise")


@router.get("/feed.xml")
async def feed():
    """Field Notes as RSS, both languages."""
    if _off():
        return Response("Not found", status_code=404)
    from datetime import datetime, timezone
    from email.utils import format_datetime
    from xml.sax.saxutils import escape
    base = config.PUBLIC_URL()
    items = "".join(
        f"<item><title>{escape(n['title'])}</title><link>{escape(base + note_url(n))}</link>"
        f"<guid>{escape(base + note_url(n))}</guid><description>{escape(n['desc'])}</description>"
        f"<pubDate>{format_datetime(datetime.fromisoformat(n['date']).replace(hour=12, tzinfo=timezone.utc))}</pubDate></item>"
        for n in NOTES)
    body = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Valeria One — Field Notes</title>'
            f"<link>{escape(base)}/notes</link><description>Practical notes for running a small business.</description>{items}</channel></rss>")
    return Response(body, media_type="application/rss+xml")
