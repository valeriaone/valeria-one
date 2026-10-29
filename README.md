# Valeria One

**The back office for any business, in any country, on any channel.**

![Valeria One: AI that answers with your real prices and hours](docs/screenshots/1_ai_reply.png)

Across most of the world a Facebook page *is* the website and WhatsApp *is* the front desk. Millions of
small businesses sell that way, but nobody gave them the office behind it: leads get buried in DMs,
follow-ups are forgotten, and the business owns nothing if the page goes down.

Valeria One is that office:

- **Sign up with the accounts you already have.** *Continue with Facebook* (or Google): no form, no password;
  the business arrives filled in from its Facebook Page (name, phone, email, address, hours) with its Messenger
  and Instagram already in the inbox. Then one **Connect** screen: WhatsApp, Facebook + Instagram, email,
  payments, calendar, website, TikTok, each a tap or a short form, all on the business's own accounts.
- **One inbox for every channel.** WhatsApp, Messenger, Instagram, SMS (any provider), email, Telegram,
  or anything else through a signed webhook. The core never knows which pipe a message used.
- **Customers.** One record per person, however they reach you, with consent tracked the way each
  country's law requires.
- **Deals and pipeline** in the words of your trade: a salon's *packages*, a contractor's *jobs*,
  a clinic's *treatment plans*.
- **Appointments** with no double-booking, and a **public booking page** (`/b/your-business`): customers pick a
  service, a day and a time on their phone; it is checked again under a lock so two people never get the same slot.
- **A website for businesses that have none** (`/s/your-business`): built from services, prices, hours and booking,
  with WhatsApp / Instagram / Facebook buttons, a map, a contact form straight into the inbox, and schema.org
  data so search engines understand it.
- **Payments with the business's own account**, per country: Stripe, Mercado Pago (incl. Pix), PayMongo (GCash,
  Maya), Paystack, Razorpay (incl. UPI), M-Pesa (STK push), MTN MoMo, or bank transfer. Payment links sent in the chat; provider webhooks verified
  and applied once. Valeria One never holds the money.
- **Quotes with e-signature**: itemised, the country's tax added, frozen with a SHA-256 fingerprint; the customer
  signs on their phone by typing their name; who, when, IP, device and fingerprint are kept, under the country's
  e-signature law (ESIGN/UETA, eIDAS, Código de Comercio…).
- **Follow-ups and campaigns**: reminders before appointments, thank-yous after, welcomes, deal-won messages, and
  campaigns to customers who agreed — through one outbox, with quiet hours, consent and WhatsApp template rules.
- **Booking inside the chat** on any channel: "quiero una cita" / "book" / "予約" walks the customer through service,
  day and time (tap-to-pick lists on WhatsApp, numbered options elsewhere), in their language.
- **Two-way Google Calendar**: appointments appear in the owner's calendar; time they block there is never
  offered on the booking page (`python -m valeria_one.jobs calendar` every minute pushes changes).
- **AI replies with the business's own key** (Google Gemini — free —, Anthropic Claude or OpenAI): suggests
  replies or answers on its own using only the business's services, prices, hours and booking link, in the
  customer's language; hands the chat to a person when unsure, upset or off-topic. A business assistant, not a
  general chatbot.
- **Comment to private message**: "precio?" under a Facebook or Instagram post gets one private answer by keyword rules.
- **Missed-call text-back** on the business's own Twilio number: the owner's phone rings first (optional); if nobody
  answers, the caller gets a text in seconds and the conversation lands in the inbox.
- **Review requests**: the same polite ask to every customer (no gating), one tap or after each appointment, once
  every 90 days, with counted clicks to the business's Google review page.
- **Any country, from day one.** Language, currency, tax, phone format, time zone, messaging law,
  privacy law, e-signature law and invoicing live in a *country pack* (a YAML file), not in code. A country
  without one still signs up: its pack is made from world data (Unicode CLDR, libphonenumber) with strict
  defaults (marketing only with consent, none at night, no tax added, bank transfer or cash to get paid), and
  the server's admin is told when the first business from it arrives. The owner picks any currency, any of the
  screen languages, and can download all the business's data at any time; every business gets its own
  privacy notice page.
- **A public API** with per-business keys, an audit trail of every action, and privacy erasure built in.
- **Outgoing webhooks**: every event, signed, delivered from a transactional outbox with retries for a day.
- **Teams**: invite by link; staff work the inbox, customers, deals and calendar; owners keep settings, channels,
  keys, AI and the team. One person can belong to several businesses and switch between them.
- **Only what you use.** Every part beyond the inbox and customers is a *block* the owner switches on or off;
  off means hidden and silent (no reminders, no auto-replies, no public page). And every business can call
  things its own words: *members*, *jobs*, *patients*, its own pipeline stages.
- **Works without AI.** Rules handle booking, reminders and comment replies; AI is optional, with your own key.
- **Your own server in five minutes**, with nightly backups and updates that roll back by themselves if they
  fail. See [docs/SELF_HOST.md](docs/SELF_HOST.md).
- **For hosts and agencies**: a [Partner API](docs/PARTNER_API.md) to create, pause, resume and bill the
  businesses you run for your customers, from your own control panel.

Valeria One is built to sit behind Meta's business messaging: Meta's Business Agent answers the chat;
Valeria One runs the business after it. See [docs/META.md](docs/META.md).

Created by **Tadewan Pascua** — tad.pascua@valai.mx

## Screens

Business owners get a web app (desktop and phone, light and dark) in **11 languages** — English, Español, Português,
Français, Deutsch, Filipino, Bahasa Indonesia, हिन्दी, العربية (right-to-left), 日本語, 中文 — following each reader's own
language (a Japanese employee of a Mexican salon sees Japanese). Add a language by adding one file in
`valeria_one/web/locales/` (translations welcome; native-speaker reviews especially):
**Today** (who is waiting for an answer, today's appointments, open pipeline) · **Connect** (every account in one
screen, and an invite link to share) · **Inbox** (every channel in
one list, the country's sending rules explained in plain words) · **Customers** · **Pipeline** (a board in the
words of your trade) · **Calendar** · **Channels** · **Settings** (your country's rules, API keys).

**Projects** — for any business that delivers work in stages (interior design, architecture, remodelling, events,
contractors): rooms or areas with measurements and photos, selections the customer approves or declines from their
phone, a live budget meter, quotes made from the approved pieces, purchase orders to suppliers, tasks, install and
punch lists, hours, and a **customer portal** at `/portal/<token>` (no password: the link is sent by WhatsApp).
**Design studio** (on for the interior-design pack, a switch for everyone else): free-form mood boards, an **AI
visualizer** that redesigns the customer's real room from a photo (with the business's own Gemini or OpenAI key),
2D floor plans to scale, colour palettes taken from photos, a one-tap client presentation (PDF) and a spec sheet
for installers. **AI team**: design assistant, project coordinator, collections and marketing helpers that draft
— a person always sends. Finished projects can go on the business's website, with an optional "see your space in
our style" preview for visitors.

## Screenshots

| | |
|---|---|
| ![Booking inside the chat](docs/screenshots/2_chat_booking.png) | ![Booking page](docs/screenshots/3_booking_page.png) |
| ![Quotes signed on the phone](docs/screenshots/4_quote_signed.png) | ![Any language](docs/screenshots/5_languages.png) |
| ![A website for businesses that only had a Facebook page](docs/screenshots/6_website.png) | |

## Try it

**Live sandbox: https://sandbox.valai.mx** — open a demo business in one click, or get your own.
It is filled with fake businesses (a salon in Mérida, a dentist in Chicago, a repair
service in Manila, a realtor in São Paulo, a café in Tokyo, a florist in London, a gym in Lagos, a law
firm in Bogotá, a yoga studio in Berlin). Nothing you do there sends a real message.

```bash
# get your own throwaway business and key
curl -X POST https://sandbox.valai.mx/v1/sandbox/signup -H 'Content-Type: application/json' \
     -d '{"name": "My Shop", "country": "PH", "industry": "shop"}'

# a customer writes in
curl -X POST https://sandbox.valai.mx/v1/sandbox/simulate -H "Authorization: Bearer vo_test_…" \
     -H 'Content-Type: application/json' -d '{"from": "cust-1", "name": "Maria", "body": "Magkano po?"}'

# see the inbox, answer
curl https://sandbox.valai.mx/v1/conversations -H "Authorization: Bearer vo_test_…"
```

Interactive API docs: `https://sandbox.valai.mx/docs`.

## Use it free

The hosted Valeria One at [valeriaone.valai.mx](https://valeriaone.valai.mx) is free for everyone, in any
country: everything included, for the whole team, no card.

## Run it yourself

On a fresh Ubuntu or Debian server:

```bash
curl -fsSL https://raw.githubusercontent.com/valeriaone/valeria-one/main/deploy/install.sh | sudo bash
```

It prints a one-time link to create the first account. Domain + HTTPS, backups, restore and updates:
[docs/SELF_HOST.md](docs/SELF_HOST.md). One-click images for cloud marketplaces: [deploy/marketplace](deploy/marketplace/README.md).

By hand with Docker: `cp .env.example .env`, fill it in, `docker compose up -d` (the database is migrated on start).

For development, without Docker: Python 3.11+, PostgreSQL 13+.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/alembic upgrade head
.venv/bin/uvicorn valeria_one.api:app
.venv/bin/pytest
```

## Packs

| | |
|---|---|
| Countries | reviewed packs: MX, US, PH, BR, CO, ES, DE, GB, IN, NG, KE, UG, AU, JP · every other country: safe defaults |
| Industries | generic, salon, clinic, contractor, realtor, shop, restaurant, fitness, professional |
| Channels | whatsapp, messenger, instagram, sms (Twilio), email, telegram, viber, webhook (anything), sandbox |

Add a country: copy `valeria_one/packs/countries/MX.yaml`, change the values, open a pull request.
The legal fields in every pack are a starting point (`reviewed_by_counsel: false`) — not legal advice.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

Valeria One is free software under the **GNU AGPL-3.0** ([LICENSE](LICENSE)). If you run a modified
version as a service, you must offer your users its source code. **Commercial licenses** are available
from the author for use without the AGPL obligations.

"Valeria One" is a trademark of Tadewan Pascua ([TRADEMARK.md](TRADEMARK.md)).

## Contact

Tadewan Pascua, creator of Valeria One — **tad.pascua@valai.mx** · https://www.linkedin.com/in/tad-pascua-a1b38937/
