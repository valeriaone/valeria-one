"""Fill a SANDBOX server with fake businesses around the world, so anyone can try Valeria One in a minute.

    python -m valeria_one.sandbox.seed            # wipe sandbox businesses, create the demo set, print keys
Every business here is sandbox=True: they can only use the 'sandbox' channel; nothing real is ever sent.
Run nightly (systemd timer / cron) to reset whatever visitors changed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

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
RULES = {   # a comment rule per language, so "precio?" under a post gets a private answer in the sandbox
    "es": (["precio", "cuanto", "costo", "info"], "¡Hola {name}! Gracias por tu comentario. Te escribo por aquí con precios y horarios. ¿Qué día te acomoda?"),
    "en": (["price", "how much", "cost", "info"], "Hi {name}! Thanks for your comment. Here are our prices and times — which day works for you?"),
    "pt": (["preco", "quanto", "valor", "info"], "Oi {name}! Obrigado pelo comentário. Te mando aqui os valores e horários — qual dia fica bom?"),
    "ja": (["値段", "いくら", "料金", "予約"], "{name}様、コメントありがとうございます。料金とご予約の詳細をこちらでお送りします。"),
    "de": (["preis", "kosten", "wie viel", "info"], "Hallo {name}! Danke für deinen Kommentar. Hier sind unsere Preise und Zeiten — welcher Tag passt dir?"),
}
SITES = {   # what each demo business says on its website
    "Salón Lupita": ("Uñas, cabello y novias en el centro de Mérida", "Somos un salón familiar desde 2012. Agenda en línea o escríbenos por WhatsApp.", "Calle 60 #500, Centro, Mérida, Yucatán", "#c2185b"),
    "Bright Smile Dental": ("Gentle family dentistry in Chicago", "New patients welcome. Cleanings, whitening and emergencies — book online in a minute.", "233 S Wacker Dr, Chicago, IL", "#0e7490"),
    "Manila Fix-It Services": ("Aircon, plumbing and repaint — Metro Manila", "Licensed technicians, fair prices, same-week visits. Message us for a free estimate.", "Makati City, Metro Manila", "#1d4ed8"),
    "Casa Bela Imóveis": ("Apartamentos e casas em São Paulo e Rio", "Atendimento humano, visitas agendadas online e financiamento sem complicação.", "Rua dos Pinheiros 1000, São Paulo", "#15803d"),
    "Kissa Hanami": ("季節の料理と自家焙煎コーヒー", "下北沢の小さな喫茶店です。ご予約はオンラインで承ります。", "東京都世田谷区北沢2丁目", "#7c2d12"),
    "Bloom & Stem Florist": ("Fresh flowers, delivered across North London", "Same-day bouquets and wedding flowers. Order by message or pick up in Camden.", "Camden High St, London", "#be185d"),
    "Lagos Strong Fitness": ("Train strong in Lekki", "Classes every day, personal training and a free trial session.", "Admiralty Way, Lekki, Lagos", "#b45309"),
    "Despacho Ruiz Abogados": ("Asesoría legal para empresas en Bogotá", "Constitución de sociedades, contratos y trámites. Primera consulta con cita.", "Cra. 7 #71-21, Bogotá", "#334155"),
    "Nakato Fresh Market": ("Fresh produce delivered across Kampala", "Matooke, fruit, vegetables and eggs from the farm to your door. Order on WhatsApp, pay with MTN MoMo or Airtel Money.", "Nakasero Market, Kampala", "#15803d"),
    "Yoga Haus Berlin": ("Yoga für alle in Prenzlauer Berg", "Vinyasa, Yin und Anfängerkurse. Probestunde online buchen.", "Kastanienallee 10, Berlin", "#4d7c0f"),
}
SERVICES = {'Salón Lupita': [('Uñas acrílicas', 90, 45000), ('Corte y peinado', 60, 35000), ('Keratina', 120, 120000)], 'Bright Smile Dental': [('New patient cleaning', 60, 18000), ('Whitening consult', 30, 0)], 'Manila Fix-It Services': [('Aircon cleaning', 90, 150000), ('Site visit & estimate', 60, 0)], 'Casa Bela Imóveis': [('Visita a imóvel', 60, None), ('Avaliação do seu imóvel', 45, 0)], 'Kissa Hanami': [('テーブル予約 (2名)', 90, None), ('テーブル予約 (4名)', 120, None)], 'Bloom & Stem Florist': [('Bouquet pickup', 15, None), ('Wedding flowers consult', 45, 0)], 'Lagos Strong Fitness': [('Trial class', 60, 0), ('Personal training', 60, 1500000)], 'Despacho Ruiz Abogados': [('Primera consulta', 60, 15000000)], 'Yoga Haus Berlin': [('Probestunde', 75, 0), ('Vinyasa', 75, 1800)], 'Nakato Fresh Market': [('Weekly veg box (delivery)', 30, 85000), ('Crate of eggs (30)', 15, 18000), ('Same-day delivery, Kampala', 30, 5000)]}
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
    {"name": "Yoga Haus Berlin", "country": "DE", "industry": "fitness", "timezone": "Europe/Berlin",
     "people": [("Lena Fischer", "+4915112345678", "Hallo! Gibt es eine Probestunde am Mittwoch?")],
     "deals": [("10er-Karte — Lena", 0, 14000, "trial")],
     "appts": [("Probestunde Vinyasa", 0, 2, 18, 75)]},
]


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
            acc = X.connect(conn, biz, "seed", channel="sandbox", name="Sandbox inbox")
            BK.save_settings(conn, biz, "seed", enabled=True, slug=BK.slugify(spec["name"]),
                             hours=LATE if spec["industry"] == "restaurant" else WEEK, notice_minutes=60, days_ahead=30)
            for sname, mins, price in SERVICES.get(spec["name"], []):
                BK.add_service(conn, biz, "seed", name=sname, minutes=mins, price_minor=price)
            head, about, addr, color = SITES[spec["name"]]
            biz = ST.save(conn, dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one()),
                          "seed", enabled=True, headline=head, about=about, address=addr, color=color,
                          whatsapp=spec["people"][0][1] or "", instagram=BK.slugify(spec["name"]).replace("-", "")[:30])
            AIS.save(conn, biz, "seed", provider="demo", mode="auto", instructions=AI_NOTES.get(biz["language"], AI_NOTES["en"]))
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
        svc_list = SERVICES.get(spec["name"], [])
        if svc_list and ids:
            with db.tx() as conn:
                QS.create(conn, biz, "seed", title=svc_list[0][0], contact_id=ids[0], valid_days=30,
                          lines=[{"description": n, "qty": 1, "unit_minor": p or 0} for n, _, p in svc_list[:3]])
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
