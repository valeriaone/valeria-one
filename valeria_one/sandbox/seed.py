"""Fill a SANDBOX server with fake businesses around the world, so anyone can try Valeria One in a minute.

    python -m valeria_one.sandbox.seed            # wipe sandbox businesses, create the demo set, print keys
Every business here is sandbox=True: they can only use the 'sandbox' channel; nothing real is ever sent.
Run nightly (systemd timer / cron) to reset whatever visitors changed.
"""
from __future__ import annotations

import json
import secrets
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from .. import config, db, phones, packs
from ..channels import HookRequest
from ..services import businesses as B
from ..services import ai as AIS
from ..services import booking as BK
from ..services import comments as CM
from ..services import followups as FU
from ..services import payments as PM
from ..services import quotes as QS
from ..services import site as ST
from ..services import contacts as C
from ..services import deals as D
from ..services import inbox as X

AI_NOTES = {   # what each demo business tells its AI, in its own words
    "es": "Somos un negocio pequeño y amable. Aceptamos efectivo, tarjeta y transferencia. Si alguien pide descuento, di que lo platica la dueña.",
    "en": "We're a small, friendly team. We take cards and cash. For discounts or insurance questions, a person will answer.",
    "pt": "Somos uma equipe pequena e simpática. Aceitamos Pix, cartão e dinheiro. Descontos: uma pessoa responde.",
    "ja": "小さなお店です。現金・カード可。割引のご相談はスタッフがお答えします。",
    "de": "Wir sind ein kleines, freundliches Team. Karte und bar möglich. Rabatte bespricht eine Person.",
}
REMIND = {   # a day-before reminder per language
    "es": "Hola {name}, te esperamos {when} en {business}. Si necesitas cambiar, contesta este mensaje.",
    "en": "Hi {name}, see you {when} at {business}. Need to change it? Just reply here.",
    "pt": "Oi {name}, te esperamos {when} na {business}. Precisa mudar? É só responder.",
    "ja": "{name}様、{when}に{business}でお待ちしております。変更はこのメッセージにご返信ください。",
    "de": "Hallo {name}, wir sehen uns {when} bei {business}. Ändern? Einfach antworten.",
}
REPLY = {   # money02: the demo shows Valeria at work — one reply of hers to each sample customer (sample data, sandbox only)
    "es": "¡Hola {name}! Con gusto te ayudo. Te paso precios y horarios, ¿te aparto un lugar?",
    "en": "Hi {name}! Happy to help. Here are prices and times. Shall I book you in?",
    "pt": "Oi {name}! Com prazer te ajudo. Te passo preços e horários, quer que eu reserve?",
    "ja": "{name}様、お問い合わせありがとうございます。料金と空き時間をご案内します。ご予約しましょうか？",
    "de": "Hallo {name}! Gern helfe ich. Hier sind Preise und Zeiten. Soll ich dich eintragen?",
}
DEPOSIT = {"es": "Anticipo", "en": "Deposit", "pt": "Sinal", "ja": "前金", "de": "Anzahlung"}
RULES = {   # a comment rule per language, so "precio?" under a post gets a private answer in the sandbox
    "es": (["precio", "cuanto", "costo", "info"], "¡Hola {name}! Gracias por tu comentario. Te escribo por aquí con precios y horarios. ¿Qué día te acomoda?"),
    "en": (["price", "how much", "cost", "info"], "Hi {name}! Thanks for your comment. Here are our prices and times — which day works for you?"),
    "pt": (["preco", "quanto", "valor", "info"], "Oi {name}! Obrigado pelo comentário. Te mando aqui os valores e horários — qual dia fica bom?"),
    "ja": (["値段", "いくら", "料金", "予約"], "{name}様、コメントありがとうございます。料金とご予約の詳細をこちらでお送りします。"),
    "de": (["preis", "kosten", "wie viel", "info"], "Hallo {name}! Danke für deinen Kommentar. Hier sind unsere Preise und Zeiten — welcher Tag passt dir?"),
}
SITES = {   # what each demo business says on its website
    "Reflect Aesthetics": ("Aesthetic clinic & surgery by Dr. Ken Romero, Angeles City", "Facials, anti-acne, slimming and laser treatments, packages and token collections, plus Dr. Romero's procedures: Botox, fillers, threads and facial plastic surgery. Every treatment begins with a consultation.", "273 2nd Floor, R.L. Building, Sto. Rosario, San Jose, Angeles City, Pampanga", "#5f7f6b"),   # reflect01 (Tad 2026-11-09)
    "Salón Lupita": ("Uñas, cabello y novias en el centro de Mérida", "Somos un salón familiar desde 2012. Agenda en línea o escríbenos por WhatsApp.", "Calle 60 #500, Centro, Mérida, Yucatán", "#c2185b"),
    "Bright Smile Dental": ("Gentle family dentistry in Chicago", "New patients welcome. Cleanings, whitening and emergencies — book online in a minute.", "233 S Wacker Dr, Chicago, IL", "#0e7490"),
    "Manila Fix-It Services": ("Aircon, plumbing and repaint — Metro Manila", "Licensed technicians, fair prices, same-week visits. Message us for a free estimate.", "Makati City, Metro Manila", "#1d4ed8"),
    "Casa Bela Imóveis": ("Apartamentos e casas em São Paulo e Rio", "Atendimento humano, visitas agendadas online e financiamento sem complicação.", "Rua dos Pinheiros 1000, São Paulo", "#15803d"),
    "Kissa Hanami": ("季節の料理と自家焙煎コーヒー", "下北沢の小さな喫茶店です。ご予約はオンラインで承ります。", "東京都世田谷区北沢2丁目", "#7c2d12"),
    "Bloom & Stem Florist": ("Fresh flowers, delivered across North London", "Same-day bouquets and wedding flowers. Order by message or pick up in Camden.", "Camden High St, London", "#be185d"),
    "Lagos Strong Fitness": ("Train strong in Lekki", "Classes every day, personal training and a free trial session.", "Admiralty Way, Lekki, Lagos", "#b45309"),
    "Despacho Ruiz Abogados": ("Asesoría legal para empresas en Bogotá", "Constitución de sociedades, contratos y trámites. Primera consulta con cita.", "Cra. 7 #71-21, Bogotá", "#334155"),
    "Nakato Fresh Market": ("Fresh produce delivered across Kampala", "Matooke, fruit, vegetables and eggs from the farm to your door. Order on WhatsApp, pay with MTN MoMo or Airtel Money.", "Nakasero Market, Kampala", "#15803d"),
    "Decora con Hugo": ("Cabeceras y celosías cortadas a la medida en Mérida", "Cabeceras, celosías para escalera, divisores y cuadros en madera, cortados en CNC. Cotización sin costo a domicilio y pagas al recibir.", "Calle 83 x 42 y 42A, Centro, Mérida", "#b07a45"),   # hugo01
    "Tejas y Fachadas de Yucatán": ("Tejas, piedra, mármol y ónix en Mérida", "Teja, tabique, celosía, piedrín, mármol y ónix. Cotiza por m² y pide por WhatsApp; dos sucursales en Mérida.", "Calle 65 #671 x 14 y 16, Col. Emilio Portes Gil, Mérida", "#b5502b"),   # tejas01
    "Rodrigo Sánchez Interiorismo": ("Diseño de interiores y visualización 3D", "Interiores cálidos con chukum, madera y fibras naturales. Manda una foto de tu espacio y te mostramos cómo se vería; renders, planos y obra.", "Yucatán, México", "#8a6a4b"),   # dsn01
    "Malik Hasnain": ("Private chauffeur in Corona, California", "Black cars and a chauffeur in a suit for LAX, Ontario and John Wayne. Fixed price before you book; se habla español.", "Corona, CA", "#111111"),
    "Armor Gym": ("Entrena en Armor Gym, Mérida", "Pesas, cardio y clases con coach. Reserva tu clase de prueba, paga tu mensualidad en línea y te recordamos por WhatsApp.", "Calle 80 #520, Centro, Mérida", "#e10600"),
    "Up Fitness": ("Entrena en Up Fitness, Mérida", "Pesas, cardio y clases con coach. Reserva tu clase de prueba, paga tu mensualidad en línea y te recordamos por WhatsApp.", "Calle 7 #544, Altabrisa, Mérida", "#ff7a00"),
    "B-Fitness GYM": ("Entrena en B-Fitness GYM, Mérida", "Pesas, cardio y clases con coach. Reserva tu clase de prueba, paga tu mensualidad en línea y te recordamos por WhatsApp.", "Calle 9 #217, García Ginerés, Mérida", "#1e90ff"),
    "Thunder Gym": ("Entrena en Thunder Gym, Mérida", "Pesas, cardio y clases con coach. Reserva tu clase de prueba, paga tu mensualidad en línea y te recordamos por WhatsApp.", "Av. Itzaes x 49 y 51, Centro, Mérida", "#ffd400"),
    "Prime Fitness Mérida": ("Entrena en Prime Fitness, Mérida", "Pesas, cardio y clases con coach. Reserva tu clase de prueba, paga tu mensualidad en línea y te recordamos por WhatsApp.", "Plaza Santa Mónica, Las Américas, Mérida", "#c9a227"),
    # gym04 (Tad 2026-11-05 «build the demos for the 6 and send»): the Monterrey gyms we made sample sites for (scout ~/backups/gym04)
    "Impulse Fitness Studio": ("Entrena en Impulse Fitness Studio, San Pedro", "Clases con coach, paquetes y clases en línea. Reserva tu lugar, paga tu paquete y te recordamos por WhatsApp.", "Plaza QIN, Calz. San Pedro 603, San Pedro Garza García", "#e4007c"),
    "2M Calisthenics Gym": ("Calistenia en 2M, Monterrey", "Calistenia con coach para todos los niveles. Reserva tu clase de prueba, paga tu membresía en línea y te recordamos por WhatsApp.", "2M Pueblo Serena, Monterrey", "#f5b700"),
    "CrossFit Absoluto": ("CrossFit Absoluto, Monterrey", "WOD diario con coach, preparación HYROX y comunidad. Reserva tu clase de prueba y paga tu mensualidad en línea.", "Av. Lázaro Cárdenas 2906, Mirador Residencial, Monterrey", "#d62828"),
    "Fitality": ("Fitality, entrenamiento híbrido en San Pedro", "Club oficial de entrenamiento HYROX: fuerza, resistencia y simulaciones. Reserva tu clase y paga tu plan en línea.", "Calzada del Valle 406, piso 2, San Pedro Garza García", "#00b4d8"),
    "Anatomy Gym": ("Anatomy Gym, San Pedro", "Pilates, box, indoor cycle, bootcamp, yoga y barre. Reserva tu clase y compra tu plan en línea, sin esperar respuesta por WhatsApp.", "Plaza UMA, Av. Gómez Morín 2005, San Pedro Garza García", "#9b5de5"),
    "Yoga Haus Berlin": ("Yoga für alle in Prenzlauer Berg", "Vinyasa, Yin und Anfängerkurse. Probestunde online buchen.", "Kastanienallee 10, Berlin", "#4d7c0f"),
}
GYM_SERVICES = [("Clase de prueba", 60, 0), ("Visita", 60, 8000), ("Mensualidad", 30, 55000), ("Entrenamiento personal", 60, 35000)]
GYMS = ['Armor Gym', 'Up Fitness', 'B-Fitness GYM', 'Thunder Gym', 'Prime Fitness Mérida']   # gym02: the 5 Mérida gyms we made sample sites for
GYM_TOOLS = {"pos": True, "inventory": True, "voice_agent": True, "affiliates": True, "tiktok": True, "comments": True,
             "directory": True, "staff": True, "members": True}   # on top of the fitness kit: register, shakes, calls, referrals…
GYM_PEOPLE = [[("Andrea Pech", "+529991112233", "Hola, ¿cuánto cuesta la mensualidad? ¿Tienen clase de prueba?"),
               ("Luis Canché", "+529992223344", "¿A qué hora abren el sábado? Quiero ir a pesas."),
               ("Karla Dzib", "+529993334455", "Buenas, ¿tienen entrenador personal? Quiero bajar de peso.")],
              [("Daniela Euán", "+529994445566", "¿Hay clases de funcional en la mañana?"),
               ("Jorge May", "+529995556677", "¿Cuánto es la anualidad? Quiero pagar el año."),
               ("Paola Ku", "+529996667788", "Hola! ¿Puedo ir a una clase de prueba mañana?")]]
GYMS_MTY = ['Impulse Fitness Studio', '2M Calisthenics Gym', 'CrossFit Absoluto', 'Fitality', 'Anatomy Gym']   # gym04: Monterrey
GYM_SERVICES_MTY = {
    'Impulse Fitness Studio': [("Clase de prueba", 50, 0), ("Clase suelta", 50, 25000), ("Paquete 8 clases", 50, 160000), ("Mensualidad", 30, 220000)],
    '2M Calisthenics Gym': [("Clase de prueba", 60, 0), ("Visita", 60, 20000), ("Mensualidad", 30, 170000), ("Clase personal de calistenia", 60, 50000)],
    'CrossFit Absoluto': [("Clase de prueba", 60, 0), ("Drop-in", 60, 25000), ("Mensualidad", 30, 180000), ("Simulacro HYROX", 90, 60000)],
    'Fitality': [("Clase de prueba", 60, 0), ("Clase suelta", 60, 35000), ("Mensualidad", 30, 280000), ("Simulación HYROX", 90, 80000)],
    'Anatomy Gym': [("Clase de prueba", 50, 0), ("Clase suelta", 50, 30000), ("Mensualidad", 30, 250000), ("Semestral", 30, 1300000)],
}
GYM_PEOPLE_MTY = [[("Mariana Garza", "+528111112233", "Hola, ¿cuánto cuesta la mensualidad? ¿Tienen clase de prueba?"),
                   ("Rodrigo Treviño", "+528122223344", "¿A qué hora es la primera clase el sábado?"),
                   ("Sofía Villarreal", "+528133334455", "Buenas, ¿tienen paquetes de clases? Quiero empezar este lunes.")],
                  [("Diego Elizondo", "+528144445566", "¿Hay clase en la mañana antes del trabajo?"),
                   ("Valeria Cantú", "+528155556677", "¿Cuánto es la mensualidad? Quiero pagar con tarjeta."),
                   ("Andrés Salinas", "+528166667788", "Hola! ¿Puedo ir a una clase de prueba mañana?")]]
SERVICES = {'Salón Lupita': [('Uñas acrílicas', 90, 45000), ('Corte y peinado', 60, 35000), ('Keratina', 120, 120000)], 'Bright Smile Dental': [('New patient cleaning', 60, 18000), ('Whitening consult', 30, 0)], 'Manila Fix-It Services': [('Aircon cleaning', 90, 150000), ('Site visit & estimate', 60, 0)], 'Casa Bela Imóveis': [('Visita a imóvel', 60, None), ('Avaliação do seu imóvel', 45, 0)], 'Kissa Hanami': [('テーブル予約 (2名)', 90, None), ('テーブル予約 (4名)', 120, None)], 'Bloom & Stem Florist': [('Bouquet pickup', 15, None), ('Wedding flowers consult', 45, 0)], 'Lagos Strong Fitness': [('Trial class', 60, 0), ('Personal training', 60, 1500000)], 'Despacho Ruiz Abogados': [('Primera consulta', 60, 15000000)], 'Yoga Haus Berlin': [('Probestunde', 75, 0), ('Vinyasa', 75, 1800)], 'Armor Gym': GYM_SERVICES, 'Up Fitness': GYM_SERVICES, 'B-Fitness GYM': GYM_SERVICES, 'Thunder Gym': GYM_SERVICES, 'Prime Fitness Mérida': GYM_SERVICES, 'Decora con Hugo': [('Visita para medir (sin costo)', 45, 0), ('Cabecera queen', 60, 280000), ('Celosía para escalera (m²)', 60, 230000), ('Instalación a domicilio', 90, None)], 'Tejas y Fachadas de Yucatán': [('Teja de barro (millar)', 30, 1400000), ('Piedrín (m²)', 15, 38000), ('Mármol oro 30×30 (m²)', 15, 52000), ('Recoger pedido en sucursal', 15, None)], 'Rodrigo Sánchez Interiorismo': [('Consulta en sitio', 60, 120000), ('Asesoría por videollamada', 60, 80000), ('Render por espacio', 60, 350000), ('Proyecto integral (cotización)', 90, None)], 'Malik Hasnain': [('Airport transfer · Ontario (ONT)', 45, 9500), ('Airport transfer · LAX', 90, 19500), ('SUV by the hour (3 h minimum)', 180, 37500)], 'Nakato Fresh Market': [('Weekly veg box (delivery)', 30, 85000), ('Crate of eggs (30)', 15, 18000), ('Same-day delivery, Kampala', 30, 5000)]}
SERVICES["Reflect Aesthetics"] = [   # reflect01: their printed price boards (Tad's photos 2026-11-09), pesos → centavos
    ('Consultation with Dr. Ken Romero', 45, None),
    ('Reflect Essentials', 60, 50000),
    ('Reflect Advanced', 60, 100000),
    ('The Reflect Signature', 60, 189900),
    ('Reflect Shine Aqua', 60, 200000),
    ('Wart removal (from)', 60, 149900),
    ('Photodynamic therapy', 60, 34900),
    ('High frequency laser', 60, 34900),
    ('Microneedling', 60, 250000),
    ('Reflect Clarity', 60, 99900),
    ('Reflect Clarity Plus', 60, 180000),
    ('The Reflect Clarity Pro', 60, 250000),
    ('Reflect Clarity Shot (per pimple)', 15, 20000),
    ('Meso therapy (fat melting injection)', 45, 249900),
    ('Meso V-face lift', 45, 249900),
    ('Slimming ion', 45, 34900),
    ('Lipo cavitation', 45, 34900),
    ('Radio frequency (skin tightening & lifting)', 45, 34900),
    ('HIFU V-lift (from)', 45, 499900),
    ('Cold RF V-lift (from)', 45, 149900),
    ('Anti acne laser', 45, 69800),
    ('Hyperpigmentation laser (melasma or freckles) (from)', 45, 69800),
    ('Whitening laser (from)', 45, 34900),
    ('Tattoo removal laser', 45, 249900),
    ('OPT hair removal', 45, 34900),
    ('Pico ultra whitening laser', 45, 249900),
    ('Diode ultra hair removal laser', 45, 90000),
    ('Korean carbon facial', 45, 249900),
    ('Melasma & freckles removal laser', 45, 249900),
    ('Diode underarm laser (package)', 45, 800000),
    ('OPT hair removal laser (underarm) (package)', 45, 350000),
    ('Diode hair removal laser (full legs) (package)', 45, 2300000),
    ('OPT hair removal laser (full legs) (package)', 45, 1600000),
    ('Diode hair removal laser (half legs) (package)', 45, 1500000),
    ('Pico ultra whitening laser (underarm) (package)', 45, 1000000),
    ('Deluxe Collection · 30 tokens', 30, 599900),
    ('Premium Collection · 60 tokens', 30, 1300000),
    ('Ultimate Collection · 100 tokens', 30, 2000000),
    ('Botox (on consultation)', 30, None),
    ('Dermal fillers (on consultation)', 45, None),
    ('Threads (on consultation)', 60, None),
    ('Surgical consultation: nose, eyes, chin, lips, ears', 60, None),
]
SERVICES.update(GYM_SERVICES_MTY)
WEEK = {d: [["09:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")} | {"sat": [["09:00", "14:00"]]}
LATE = {d: [["12:00", "22:00"]] for d in ("tue", "wed", "thu", "fri", "sat", "sun")}
DEMO = [
    {"name": "Salón Lupita", "country": "MX", "industry": "salon", "timezone": "America/Merida",
     "people": [("Mariana López", "+529991234567", "Hola, ¿tienen lugar para uñas el sábado?"),
                ("Fernanda Cruz", "+529992345678", "¿Cuánto cuesta el paquete de novia?"),
                ("Sofía Medina", None, "Buenas tardes, ¿hacen keratina?")],
     "deals": [("Paquete de novia — Fernanda", 1, 350000, "booked"), ("Keratina — Sofía", 2, 120000, "new"),
               ("Uñas mensuales — Mariana", 0, 45000, "won")],
     "appts": [("Uñas acrílicas", 0, 1, 10, 90), ("Prueba de peinado", 1, 3, 12, 60)]},
    {"name": "Bright Smile Dental", "country": "US", "industry": "clinic", "timezone": "America/Chicago",
     "people": [("Emily Carter", "+13125550142", "Hi, do you take new patients? I need a cleaning."),
                ("James Miller", "+13125550178", "Is teeth whitening covered by insurance?")],
     "deals": [("Whitening plan — James", 1, 45000, "quoted"), ("Cleaning + x-rays — Emily", 0, 18000, "booked")],
     "appts": [("New patient cleaning", 0, 2, 9, 60)]},
    {"name": "Manila Fix-It Services", "country": "PH", "industry": "contractor", "timezone": "Asia/Manila",
     "people": [("Maria Santos", "+639171234567", "Good day po! Can you fix a leaking aircon in Makati?"),
                ("Jose Reyes", "+639181234567", "Magkano po ang repaint ng 2-bedroom condo?")],
     "deals": [("Aircon repair — Makati", 0, 350000, "visit"), ("Condo repaint 2BR", 1, 4500000, "quoted")],
     "appts": [("Aircon site visit", 0, 1, 14, 60)]},
    {"name": "Casa Bela Imóveis", "country": "BR", "industry": "realtor", "timezone": "America/Sao_Paulo",
     "people": [("Ana Souza", "+5511987654321", "Olá! O apartamento em Pinheiros ainda está disponível?"),
                ("Carlos Lima", "+5521987654321", "Vocês aceitam financiamento pela Caixa?")],
     "deals": [("Apto Pinheiros — Ana", 0, 98000000, "showing"), ("Casa Barra — Carlos", 1, 165000000, "new")],
     "appts": [("Visita Pinheiros", 0, 2, 11, 60)]},
    {"name": "Kissa Hanami", "country": "JP", "industry": "restaurant", "timezone": "Asia/Tokyo",
     "people": [("佐藤 花子", "+819012345678", "土曜日の夜、4名で予約できますか？")],
     "deals": [("誕生日パーティー (12名)", 0, 60000, "quoted")],
     "appts": [("4名 ディナー", 0, 3, 19, 120)]},
    {"name": "Bloom & Stem Florist", "country": "GB", "industry": "shop", "timezone": "Europe/London",
     "people": [("Oliver Hughes", "+447911123456", "Hi! Can you deliver roses to Camden tomorrow?")],
     "deals": [("Anniversary roses — Camden", 0, 6500, "paid")],
     "appts": [("Delivery Camden", 0, 1, 10, 30)]},
    {"name": "Lagos Strong Fitness", "country": "NG", "industry": "fitness", "timezone": "Africa/Lagos",
     "people": [("Chidi Okafor", "+2348031234567", "How much is the monthly membership in Lekki?")],
     "deals": [("Monthly membership — Chidi", 0, 3500000, "trial")],
     "appts": [("Trial class", 0, 1, 7, 60)]},
    {"name": "Despacho Ruiz Abogados", "country": "CO", "industry": "professional", "timezone": "America/Bogota",
     "people": [("Valentina Gómez", "+573001234567", "Buenas, necesito asesoría para registrar mi empresa.")],
     "deals": [("Constitución de SAS", 0, 250000000, "meeting")],
     "appts": [("Primera reunión", 0, 2, 15, 60)]},
    {"name": "Nakato Fresh Market", "country": "UG", "industry": "shop", "timezone": "Africa/Kampala",
     "people": [("Grace Namubiru", "+256772123456", "Hello! Do you deliver matooke and avocados to Ntinda today?"),
                ("Brian Okello", "+256701234567", "How much is a crate of eggs, and can I pay with MoMo?")],
     "deals": [("Weekly veg box — Grace", 0, 85000, "paid"), ("Eggs for café — Brian", 1, 360000, "quoted")],
     "appts": [("Delivery Ntinda", 0, 1, 10, 30)]},
    {"name": "Decora con Hugo", "country": "MX", "industry": "contractor", "timezone": "America/Merida",   # hugo01 (Tad 2026-11-07)
     "people": [("Sofía Cauich", "+529991234321", "Hola! ¿Cuánto la cabecera de mandala tamaño queen?"),
                ("Carlos Chan", "+529992345432", "¿Me pueden cotizar un divisor de sala de 2 x 2.4 m?"),
                ("Mariela Puc", "+529983456543", "Estoy en Cancún, ¿llegan hasta acá con la cabecera king?")],
     "deals": [("Cabecera mandala queen — Sofía", 0, 280000, "visit"), ("Divisor de sala 4.8 m² — Carlos", 1, 1008000, "quoted"),
               ("Cabecera king blanca (gira Cancún) — Mariela", 2, 330000, "new")],
     "appts": [("Visita para medir — Sofía", 0, 1, 10, 45)],
     "tools": {"voice_agent": True, "contracts": True, "staff": True, "comments": True, "directory": True, "calendar": True,
               "pos": True, "affiliates": True, "tiktok": True}},
    {"name": "Tejas y Fachadas de Yucatán", "country": "MX", "industry": "shop", "timezone": "America/Merida",   # tejas01 (Tad 2026-11-04)
     "people": [("Jorge Canul", "+529994443322", "¿Cuánto me sale la teja de barro para 40 m² de techo?"),
                ("Andrea Poot", "+529995554433", "Hola, ¿tienen 300 tabiques para recoger el viernes?"),
                ("Arq. Luis Medina", "+529996665544", "Soy arquitecto, tengo obra de 3 casas. ¿Manejan precio por volumen?")],
     "deals": [("Teja de barro 40 m² — Jorge", 0, 705600, "quoted"), ("300 tabiques — Andrea", 1, 165000, "paid"),
               ("Mármol oro 180 m² (3 casas) — Arq. Medina", 2, 9828000, "new")],
     "appts": [("Recoger pedido — Andrea", 1, 1, 16, 15)],
     "tools": {"voice_agent": True, "contracts": True, "staff": True, "comments": True, "directory": True, "calendar": True}},
    {"name": "Rodrigo Sánchez Interiorismo", "country": "MX", "industry": "design", "timezone": "America/Merida",   # dsn01 (Tad 2026-11-04)
     "people": [("Mariana Cetina", "+529997778899", "Hola! Te mando foto de mi sala, ¿cómo se vería con tu estilo?"),
                ("Carlos Peniche", "+529998889900", "¿Cuánto cuesta diseñar cocina y sala, unos 60 m²?"),
                ("Laura Kent", "+529990001122", "Hi! I just bought a villa, do you do full interiors?")],
     "deals": [("Cocina y sala 60 m² — Carlos", 1, 2700000, "proposal"), ("Render sala — Mariana", 0, 350000, "visit"),
               ("Villa completa — Laura", 2, 18000000, "new")],
     "appts": [("Consulta en sitio — Mariana", 0, 2, 11, 60), ("Videollamada — Laura", 2, 1, 17, 60)],
     "tools": {"voice_agent": True, "affiliates": True, "tiktok": True, "comments": True, "directory": True, "staff": True,
               "campaigns": True, "emails": True, "grow": True, "funnels": True, "ads": True, "google": True, "marketplace": True}},
    {"name": "Malik Hasnain", "country": "US", "industry": "generic", "timezone": "America/Los_Angeles",   # limo01 (Tad 2026-11-04)
     "people": [("Jessica Ramirez", "+19515550143", "Hi! How much from Corona to LAX on Friday at 5 am? 2 of us, 3 bags."),
                ("Daniel Brooks", "+17145550122", "Do you do weddings? We need the SUV for 6 hours on Dec 12 in Temecula."),
                ("Priya Shah", "+19515550187", "Can you pick me up at ONT tonight at 11:40 pm? Flight WN 1234.")],
     "deals": [("Wedding SUV, 6 h — Daniel", 1, 75000, "quoted"), ("LAX Friday 5 am — Jessica", 0, 19500, "booked"),
               ("ONT pickup tonight — Priya", 2, 9500, "won")],
     "appts": [("LAX drop-off, Sedan", 0, 2, 5, 90), ("ONT pickup, flight WN 1234", 2, 0, 23, 45)],
     # limo01 (Tad «the whole valeriaone whatever applies»): on top of the any-business kit, every tool a car service uses
     "tools": {"staff": True, "pos": True, "voice_agent": True, "affiliates": True, "members": True, "tiktok": True,
               "comments": True, "directory": True}},
    *[{"name": n, "country": "MX", "industry": "fitness", "timezone": "America/Merida", "people": GYM_PEOPLE[i % 2],
       "deals": [("Mensualidad — " + GYM_PEOPLE[i % 2][0][0].split()[0], 0, 55000, "trial"),
                 ("Anualidad — " + GYM_PEOPLE[i % 2][1][0].split()[0], 1, 540000, "quoted"),
                 ("Entrenamiento personal x10 — " + GYM_PEOPLE[i % 2][2][0].split()[0], 2, 300000, "won")],
       "appts": [("Clase de prueba", 0, 1, 7, 60), ("Entrenamiento personal", 2, 2, 18, 60)], "tools": GYM_TOOLS}
      for i, n in enumerate(GYMS)],
    *[{"name": n, "country": "MX", "industry": "fitness", "timezone": "America/Monterrey", "people": GYM_PEOPLE_MTY[i % 2],
       "deals": [("Mensualidad — " + GYM_PEOPLE_MTY[i % 2][0][0].split()[0], 0, GYM_SERVICES_MTY[n][2][2], "trial"),
                 (GYM_SERVICES_MTY[n][3][0] + " — " + GYM_PEOPLE_MTY[i % 2][1][0].split()[0], 1, GYM_SERVICES_MTY[n][3][2], "quoted"),
                 (GYM_SERVICES_MTY[n][1][0] + " x10 — " + GYM_PEOPLE_MTY[i % 2][2][0].split()[0], 2, GYM_SERVICES_MTY[n][1][2] * 10, "won")],
       "appts": [("Clase de prueba", 0, 1, 7, 60), (GYM_SERVICES_MTY[n][3][0], 2, 2, 18, 60)], "tools": GYM_TOOLS}
      for i, n in enumerate(GYMS_MTY)],
    {"name": "Reflect Aesthetics", "country": "PH", "industry": "clinic", "timezone": "Asia/Manila",   # reflect01 (Tad 2026-11-09)
     "people": [("Camille Santos", "+639171234567", "Hi! How much is the Reflect Signature facial? Are you open Sunday?"),
                ("Joy Dizon", "+639181234567", "Hello po, magkano po ang underarm diode laser package? May downpayment po ba?"),
                ("Mark Villanueva", "+639191234567", "Good evening, I'd like a consultation with Dr. Romero for my nose. Saturday?")],
     "deals": [("Diode underarm laser package — Joy", 1, 800000, "quoted"), ("The Reflect Signature — Camille", 0, 189900, "booked"),
               ("Rhinoplasty consultation — Mark", 2, 0, "new")],
     "appts": [("The Reflect Signature — Camille", 0, 1, 14, 60), ("Consultation with Dr. Romero — Mark", 2, 5, 11, 45)],
     "hours": {d: [["11:00", "20:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
     "assistant": True,   # the chat on their copied website (reflectaesthetics.valai.mx) answers patients with these prices
     "ai_notes": 'We are Reflect Aesthetics by Dr. Ken Romero, M.D. (ENT–head and neck surgeon, fellowship in facial plastic aesthetic surgery), 273 2nd Floor, R.L. Building, Sto. Rosario, San Jose, Angeles City, Pampanga. Open Monday to Sunday, 11:00 AM to 8:00 PM. Prices are in Philippine pesos (₱). Meso therapy (fat melting injection): ₱2,499 — 4+1 sessions at 9,999 · downpayment 3,499 Korean carbon facial: ₱2,499 — 4+1 sessions at 9,999 · downpayment 3,499 Melasma & freckles removal laser: ₱2,499 — 4+1 sessions at 9,999 · downpayment 3,499 Diode underarm laser: ₱8,000 — 10 sessions · free hydrocortisone & deodorant · downpayment 2,500 OPT hair removal laser (underarm): ₱3,500 — 10 sessions · free hydrocortisone Diode hair removal laser (full legs): ₱23,000 — 5 sessions · free oatmeal soap, moisturizing lotion & hydrocortisone · downpayment 5,000 OPT hair removal laser (full legs): ₱16,000 — 10 sessions · free oatmeal soap, moisturizing lotion & hydrocortisone · downpayment 3,500 Diode hair removal laser (half legs): ₱15,000 — 5 sessions · free hydrocortisone & oatmeal soap · downpayment 3,000 Pico ultra whitening laser (underarm): ₱10,000 — 4 sessions · free hydrocortisone, deodorant, whitening cream & oatmeal soap · downpayment 3,000 Deluxe Collection · 30 tokens: ₱5,999 — downpayment 2,000 Premium Collection · 60 tokens: ₱13,000 — downpayment 3,500 Ultimate Collection · 100 tokens: ₱20,000 — downpayment 5,000 Token collections: you buy tokens and spend them on treatments — tokens per treatment: Diamond peel 1; Galvanic 1; Ultrasonic 1; Rejuvenating 1; High frequency 1; Photo dynamic treatment 1; Basic facial 1; BB glow 12; Microneedling 12; Oxygen facial 12; HIFU (face) 23; UA hair removal laser 1; Hyperpigmentation 2; UA whitening laser 1; Anti acne laser 2; Carbon facial 12; Melasma removal laser 12; Ultra whitening laser 12; Groin whitening laser 15. Botox, fillers, threads and surgery (nose, eyes, chin, lips, ears) are priced at the consultation with Dr. Romero. Every treatment begins with an assessment; suitability depends on the person and results vary — never promise results and never give medical advice. Offer to book a consultation or the treatment, and ask for the downpayment when the package has one.',
     "tools": {"voice_agent": True, "comments": True, "calendar": True, "followups": True, "forms": True, "staff": True,
               "directory": True, "pos": True, "campaigns": True, "contracts": True}},
    {"name": "Yoga Haus Berlin", "country": "DE", "industry": "fitness", "timezone": "Europe/Berlin",
     "people": [("Lena Fischer", "+4915112345678", "Hallo! Gibt es eine Probestunde am Mittwoch?")],
     "deals": [("10er-Karte — Lena", 0, 14000, "trial")],
     "appts": [("Probestunde Vinyasa", 0, 2, 18, 75)]},
]


STUDIO_PROJECTS = {                                           # cad01: sample projects so a design demo shows plans, rooms and stages
    "es": [("Cocina y sala, 60 m²", "Mediterráneo cálido: chukum, madera y fibras naturales",
            "Abrir la cocina a la sala, isla de 2.4 m, iluminación indirecta y muebles a la medida. Planos en AutoCAD y render en SketchUp.",
            2700000, [("Cocina", 420, 380, 270), ("Sala", 520, 450, 270)]),
           ("Villa completa", "Contemporáneo yucateco", "Interiorismo integral: 3 recámaras, terraza y alberca. Planos, renders y supervisión de obra.",
            18000000, [("Recámara principal", 450, 400, 290), ("Terraza", 800, 350, None), ("Baño principal", 300, 250, 270)])],
    "en": [("Kitchen & living room, 60 m²", "Warm Mediterranean: lime plaster, wood and natural fibers",
            "Open the kitchen to the living room, 2.4 m island, indirect lighting and built-in furniture. AutoCAD plans and SketchUp render.",
            2700000, [("Kitchen", 420, 380, 270), ("Living room", 520, 450, 270)]),
           ("Full villa", "Contemporary", "Full interiors: 3 bedrooms, terrace and pool. Plans, renders and site supervision.",
            18000000, [("Main bedroom", 450, 400, 290), ("Terrace", 800, 350, None), ("Main bathroom", 300, 250, 270)])],
}


def wipe() -> int:
    with db.tx() as conn:
        return conn.execute(delete(db.businesses).where(db.businesses.c.sandbox.is_(True))).rowcount


def seed(now: datetime | None = None) -> list[dict]:
    if not config.SANDBOX():
        raise SystemExit("refusing: VO_SANDBOX=1 is not set on this server")
    now = now or datetime.now(timezone.utc)
    out = []
    for spec in DEMO:
        pack = packs.country(spec["country"])
        with db.tx() as conn:
            biz = B.create(conn, name=spec["name"], country=spec["country"], industry=spec["industry"],
                           timezone=spec["timezone"], sandbox=True, demo=True)
            key, _ = B.new_key(conn, biz["id"], "demo")
            if spec.get("tools"):
                conn.execute(db.businesses.update().where(db.businesses.c.id == biz["id"]).values(tools=spec["tools"]))
            acc = X.connect(conn, biz, "seed", channel="sandbox", name="Sandbox inbox")
            BK.save_settings(conn, biz, "seed", enabled=True, slug=BK.slugify(spec["name"]),
                             hours=spec.get("hours") or (LATE if spec["industry"] == "restaurant" else WEEK), notice_minutes=60, days_ahead=30)
            for sname, mins, price in SERVICES.get(spec["name"], []):
                BK.add_service(conn, biz, "seed", name=sname, minutes=mins, price_minor=price)
            head, about, addr, color = SITES[spec["name"]]
            biz = ST.save(conn, dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one()),
                          "seed", enabled=True, headline=head, about=about, address=addr, color=color,
                          whatsapp=spec["people"][0][1] or "", instagram=BK.slugify(spec["name"]).replace("-", "")[:30],
                          **({"assistant": "1"} if spec.get("assistant") else {}))   # reflect01: its website chat, on any site by script tag
            AIS.save(conn, biz, "seed", provider="demo", mode="auto", instructions=spec.get("ai_notes") or AI_NOTES.get(biz["language"], AI_NOTES["en"]))
            PM.connect(conn, biz, "seed", "bank_transfer", {"bank": "Demo Bank (sandbox)", "holder": spec["name"],
                                                             "account": "0000 0000 0000 0000", "note": "Sandbox — no real money"})
            FU.create_automation(conn, biz, "seed", trigger="appointment.reminder", minutes=1440,
                                 message=REMIND.get(biz["language"], REMIND["en"]), template="appointment_reminder")
            kw, reply = RULES.get(biz["language"], RULES["en"])
            CM.create_rule(conn, biz, "seed", keywords=kw, reply=reply, channels=["messenger", "instagram", "sandbox"])
            raw_acc = conn.execute(select(db.channel_accounts).where(db.channel_accounts.c.id == acc["id"])).mappings().one()
        ids = []
        for i, (name, phone, first) in enumerate(spec["people"]):
            if phone:
                phones.e164(phone, pack)            # every demo number must be a real-format number
            X.receive(raw_acc, HookRequest("POST", "", {}, {}, json.dumps(
                {"from": f"demo-{i}", "name": name, "body": first}).encode()))
            with db.tx() as conn:
                cid = conn.execute(select(db.identities.c.contact_id).where(
                    db.identities.c.business_id == biz["id"], db.identities.c.address == f"demo-{i}")).scalar()
                if phone:
                    C.patch(conn, biz, "seed", cid, {"phone": phone})
            ids.append(cid)
        for title, who, amount, stage in spec["deals"]:
            with db.tx() as conn:
                D.create(conn, biz, "seed", {"title": title, "contact_id": ids[who], "amount_minor": amount, "stage": stage})
        with db.tx() as conn:                         # money02: Valeria's own replies (by_ai) + one paid deposit, so «Your week» shows her work
            lang = biz["language"].split("_")[0]
            for i, cid in enumerate(ids):
                cv = conn.execute(select(db.conversations.c.id).where(db.conversations.c.business_id == biz["id"],
                                                                      db.conversations.c.contact_id == cid)).scalar()
                if cv:
                    first = (spec["people"][i][0] or "").split(" ")[0]
                    conn.execute(db.messages.insert().values(business_id=biz["id"], conversation_id=cv, direction="out", kind="service",
                                                             body=REPLY.get(lang, REPLY["en"]).format(name=first), media=[],
                                                             status="sent", by_ai=True))
            if spec["deals"] and spec["deals"][0][2]:
                conn.execute(db.payments.insert().values(business_id=biz["id"], provider="sandbox", contact_id=ids[spec["deals"][0][1]],
                                                         amount_minor=spec["deals"][0][2] // 2, currency=biz["currency"],
                                                         description=f'{DEPOSIT.get(lang, DEPOSIT["en"])} · {spec["deals"][0][0]}',
                                                         token=secrets.token_urlsafe(12), attempts=0, status="paid", paid_at=func.now()))
        svc_list = SERVICES.get(spec["name"], [])
        if svc_list and ids:
            with db.tx() as conn:
                QS.create(conn, biz, "seed", title=svc_list[0][0], contact_id=ids[0], valid_days=30,
                          lines=[{"description": n, "qty": 1, "unit_minor": p or 0} for n, _, p in svc_list[:3]])
        if packs.industry(spec["industry"]).studio and ids:      # cad01: a design / architecture / engineering demo shows its Studio
            from ..services import projects as PRJ
            lang = biz["language"].split("_")[0]
            for k, (pname, style, brief, budget, rooms) in enumerate(STUDIO_PROJECTS.get(lang, STUDIO_PROJECTS["en"])):
                with db.tx() as conn:
                    pr = PRJ.create(conn, biz, "seed", {"name": pname, "contact_id": ids[min(k, len(ids) - 1)], "style": style,
                                                         "brief": brief, "budget_minor": budget,
                                                         "stage": PRJ.stage_keys(biz)[min(k + 1, len(PRJ.stage_keys(biz)) - 1)]})
                    for rname, w, l, h in rooms:
                        PRJ.add_room(conn, biz, "seed", pr["id"], {"name": rname, "width_cm": w, "length_cm": l, "height_cm": h})
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(spec["timezone"])
        for title, who, days, hour, minutes in spec["appts"]:
            local = (now.astimezone(tz) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
            with db.tx() as conn:
                D.book(conn, biz, "seed", {"title": title, "contact_id": ids[who], "starts_at": local,
                                           "ends_at": local + timedelta(minutes=minutes)})
        out.append({"business": spec["name"], "country": spec["country"], "industry": spec["industry"],
                    "business_id": str(biz["id"]), "api_key": key})
    return out


def main() -> None:
    n = wipe()
    demo = seed()
    path = os.environ.get("VO_SANDBOX_KEYS_FILE")
    if path:
        with open(path, "w") as f:
            json.dump(demo, f, ensure_ascii=False, indent=2)
    print(f"wiped {n} sandbox businesses; created {len(demo)}")
    for d in demo:
        print(f"  {d['country']}  {d['industry']:<13} {d['business']:<26} {d['api_key']}")


if __name__ == "__main__":
    main()
