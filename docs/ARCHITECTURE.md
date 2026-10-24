# Architecture

```
            WhatsApp  Messenger  Instagram  SMS  Email  Telegram  Webhook(any)  Sandbox
                 \        |          |       |     |       |          |          /
                  +---------------- channel adapters (channels/) ---------------+
                                          |
         packs/ (country + industry) --> services/  (contacts, inbox, deals, appointments)
                                          |
                                 Postgres (db.py)  +  events (audit trail)
                                          |
                                public API v1 (api/)  -- per-business keys
```

## Principles

1. **Config, never fork.** Anything that differs by country or trade is data in `packs/`. The code never
   asks "is this Mexico?" — it asks the business's country pack.
2. **The core never knows the pipe.** A channel adapter turns our `Outbound` into a provider call and a
   provider webhook into our `Inbound` / `StatusUpdate`. Adding a channel is one file.
3. **One business never sees another.** Every row carries `business_id`; every service function filters by
   it; tests prove a key from one business gets 404 on another's data.
4. **Rules are enforced on every send**, whatever the channel: marketing needs recorded consent and never
   reaches someone who unsubscribed; country quiet hours (US 21:00–08:00 local); SMS STOP stops
   everything; WhatsApp/Messenger/Instagram need an approved template outside the 24-hour reply window.
5. **Money is integers** in the currency's smallest unit. Fixed taxes (IVA, VAT, GST) are computed from
   the pack; external taxes (US sales tax) are left to the payment provider — never invented.
6. **Secrets.** Channel credentials are encrypted at rest (Fernet); API keys are stored only as SHA-256.
   Webhooks are signature-checked before anything is read.
7. **Safe to retry.** Provider message ids are stored once, so webhook replays never duplicate messages;
   delivery status only moves forward (read never goes back to delivered).
8. **Everything is on record.** `events` is an append-only trail of who did what.

## Layout

| Path | What |
|---|---|
| `valeria_one/packs/` | country + industry packs (YAML) and their validation |
| `valeria_one/channels/` | one adapter per channel |
| `valeria_one/services/` | business logic (contacts, inbox, deals, appointments, businesses) |
| `valeria_one/api/` | the public HTTP API (FastAPI; docs at `/docs`) |
| `valeria_one/money.py`, `phones.py` | money with tax, E.164 phones |
| `valeria_one/sandbox/seed.py` | the demo world for the sandbox server |
| `alembic/` | database migrations |
| `tests/` | the test suite |

## Roadmap

- Web app for business owners (inbox, pipeline board, calendar) on top of the API
- Meta: Embedded Signup (connect WhatsApp/Page/Instagram in one click), Lead Ads, comment-to-DM,
  WhatsApp Flows, catalog sync, Business Agent hand-off
- Two-way Google / Microsoft calendar, booking page
- Websites, deal rooms with e-signature, follow-up plans, campaigns, AI replies with the business's own key
- Payments: Stripe, Mercado Pago, PayMongo, Pix, UPI, Paystack — per country pack
- More channels: LINE, Viber, KakaoTalk, Zalo, RCS
- Outbound webhooks (event subscriptions), OAuth for third-party apps
