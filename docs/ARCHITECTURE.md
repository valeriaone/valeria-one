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
9. **Only what the business uses.** Beyond the inbox and customers, every part is a *block*
   (`blocks.py`) the owner switches on or off. Off means hidden on the screens and silent in the
   background (no reminders, auto-replies, comment answers or public pages); the data stays. Words are
   per business too: the industry pack gives defaults in every language, the business may rename any.
10. **The app never gets the keys to the server.** Backups and updates run on the host
    (`deploy/valeria-one`); the app only drops a request file the host watches and reads back the
    results. Updates back up first and roll back by themselves if the new version is not healthy.

## Layout

| Path | What |
|---|---|
| `valeria_one/packs/` | country + industry packs (YAML) and their validation |
| `valeria_one/channels/` | one adapter per channel |
| `valeria_one/services/` | business logic (contacts, inbox, deals, appointments, businesses) |
| `valeria_one/api/` | the public HTTP API (FastAPI; docs at `/docs`) |
| `valeria_one/money.py`, `phones.py` | money with tax, E.164 phones |
| `valeria_one/blocks.py` | the parts a business switches on, and its own words |
| `valeria_one/services/instance.py` | this server: first-time setup, admin, sign-up policy, Server page |
| `valeria_one/services/partner.py` | Partner API: hosts and agencies create, pause, bill and delete businesses |
| `deploy/` | install script, the `valeria-one` server command, systemd units, Caddy, marketplace images |
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
- More channels: LINE, KakaoTalk, Zalo, RCS (Viber and Telegram are in)
- Outbound webhooks (event subscriptions), OAuth for third-party apps

## v0.16 (mon01): own fields, recipes, boards, forms — decisions

- **Custom fields** live on the row (`contacts.custom`, `deals.custom`, JSONB keyed by the field's random key `f_xxxxxx`,
  never its name), defined in `custom_fields`. Max 20 per object. Types are fixed once made (values stay valid).
  Delete = `deleted_at`; values stay 30 days for undo, then the `recipes` job step purges them. The office assistant
  (copilot `client` tool) reads them; the AI that drafts replies *to customers* does not (internal notes stay internal).
- **CSV**: there was none (export.py is the JSON export, which now also carries the new tables). Added customers out/in
  and deals out. Import dedupes by phone/email, never imports marketing consent, and its actor (`import:…`) is not a lead.
- **Recipes** hang off `services.log()` like follow-ups/loyalty/webhooks, inside the event's transaction, one savepoint
  per recipe (a failing recipe never undoes what set it off). Idempotent: `recipe_runs` UNIQUE(recipe, dedupe=`ev:<event id>`
  or `quiet:<conversation>:<last inbound>`). Loop protection: an event caused by a recipe can't run a recipe that already
  ran for the same customer/deal in the last hour (logged as "loop"); chains cut at depth 3. They ride with the
  Follow-ups block (off = silent). "New lead" = a customer created by anyone but the team (`user:`), an import or a recipe.
- **Customer messages** from recipes are never sent directly: they become Valeria `reply` actions — a question to the
  owner (default), or queued with her 60-second undo when the recipe's "ask me first" is off and Valeria isn't paused —
  so `inbox.send` applies consent, quiet hours and the 24 h window. Someone who said STOP is skipped by the recipe itself.
  "Tell me" = a Valeria `notice` action (to linked owner phones; always on the Valeria log).
- **Board**: the pipeline already had stage columns with a dropdown, but its `.board` class collided with the mood
  board's `.board` (aspect-ratio, overflow hidden). Now `.kb` + `static/kanban.js` (pointer events, ~10 KB, no library):
  mouse drag anywhere on a card, touch from the ⠿ grip, arrow keys, "Move to…" (no JS needed). Order inside a column
  isn't stored. Tasks got `started_at` (Doing), `due_at`, `contact_id`, `deal_id`, and are for every business now
  (no longer behind the projects block). List/board choice: localStorage `vo-view:<board>`, `?view=` wins.
- **Forms**: `/form/<slug>` (the friend links own `/f/<code>`), slug = business + form name, globally unique. The
  public page draws its own body in the business's colour/photo; no Valeria One chrome or title. CSRF = a signed token
  in the page (works inside an iframe on the owner's site, where our cookie isn't sent) + honeypot + 10/hour per IP.
  Name always; phone or email required by the editor. Two separate boxes: request consent (required, kept on the
  submission) and marketing consent (optional, never pre-ticked; sets the contact's consent with its source).
- **Theme switch**: one round button, one icon (sun in dark mode, moon in light), same `vo-theme` key, `aria-pressed`.
