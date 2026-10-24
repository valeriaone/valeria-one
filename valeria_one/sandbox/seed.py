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
from ..services import booking as BK
from ..services import comments as CM
from ..services import contacts as C
from ..services import deals as D
from ..services import inbox as X

RULES = {   # a comment rule per language, so "precio?" under a post gets a private answer in the sandbox
    "es": (["precio", "cuanto", "costo", "info"], "¡Hola {name}! Gracias por tu comentario. Te escribo por aquí con precios y horarios. ¿Qué día te acomoda?"),
    "en": (["price", "how much", "cost", "info"], "Hi {name}! Thanks for your comment. Here are our prices and times — which day works for you?"),
    "pt": (["preco", "quanto", "valor", "info"], "Oi {name}! Obrigado pelo comentário. Te mando aqui os valores e horários — qual dia fica bom?"),
    "ja": (["値段", "いくら", "料金", "予約"], "{name}様、コメントありがとうございます。料金とご予約の詳細をこちらでお送りします。"),
    "de": (["preis", "kosten", "wie viel", "info"], "Hallo {name}! Danke für deinen Kommentar. Hier sind unsere Preise und Zeiten — welcher Tag passt dir?"),
}
SERVICES = {'Salón Lupita': [('Uñas acrílicas', 90, 45000), ('Corte y peinado', 60, 35000), ('Keratina', 120, 120000)], 'Bright Smile Dental': [('New patient cleaning', 60, 18000), ('Whitening consult', 30, 0)], 'Manila Fix-It Services': [('Aircon cleaning', 90, 150000), ('Site visit & estimate', 60, 0)], 'Casa Bela Imóveis': [('Visita a imóvel', 60, None), ('Avaliação do seu imóvel', 45, 0)], 'Kissa Hanami': [('テーブル予約 (2名)', 90, None), ('テーブル予約 (4名)', 120, None)], 'Bloom & Stem Florist': [('Bouquet pickup', 15, None), ('Wedding flowers consult', 45, 0)], 'Lagos Strong Fitness': [('Trial class', 60, 0), ('Personal training', 60, 1500000)], 'Despacho Ruiz Abogados': [('Primera consulta', 60, 15000000)], 'Yoga Haus Berlin': [('Probestunde', 75, 0), ('Vinyasa', 75, 1800)]}
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
