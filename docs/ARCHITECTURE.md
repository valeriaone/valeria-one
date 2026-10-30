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
| `valeria_one/kits.py` | the tool registry (every menu entry, one place), what each trade's kit shows, Add more, suggestions |
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

## v0.19 (kit01): kits — one toolbox, each trade sees its own tools first — decisions

- **One registry** (`kits.REGISTRY`): every tool's key, address, icon, group, words and the blocks it needs. The office
  menu is drawn from it (`kits.menu`), so a new tool only registers there. Core (Today, Valeria, Connect, Inbox,
  Customers, Settings, Add more, Start, Billing, Server) is always in the menu; role and blocks still apply as before.
- **A kit is data in the industry pack** (`kit:` in `packs/industries/<code>.yaml`): the tools that start ON, in menu
  order, and the menu's own words for them (locale keys `kw_…`: a gym's Customers are Members, its Calendar is
  Classes). The pack's other words and stages are reused, not copied. A trade without a kit gets the generic kit;
  adding a trade = adding one YAML. New pack: `florist` (orders, deliveries).
- **The owner's choices sit on top** (`businesses.tools`), from Add more (`/app/add`). Off only HIDES a tool (menu and
  Today); its data stays, its address still opens with "show it in my menu", and what it does by itself is still the
  blocks' job. Showing a tool switches on a block it needs, so one tap is enough.
- **Never lose what is used.** The migration (`a424ed548d9d`) pinned ON, for every business, every tool that held its
  data; changing the trade in Settings pins again (`kits.keep_in_use`) — but never over a tool the owner hid on purpose.
- **Valeria suggests** (`kits.suggestion`): plain rules on the business's own numbers (no-shows → reminders, quotes →
  invoices, regulars → loyalty), one at a time, owner only; "no thanks" is stored (`businesses.tips_dismissed`) and that
  suggestion never comes back. No AI, nothing sent.

## v0.18 (ghl01): campaigns + drips, workflows, the phone agent, invoices + subscriptions, ready setups, the app — decisions

Everything below is free and uses the business's OWN accounts (its mailbox, its Twilio/WhatsApp, its payment account,
its AI key); each part is dormant until that account is connected. One migration (`a18c0e5f7b91`).

- **Campaigns extend what was there.** `campaigns` (v0.6: a message to a tag) gained `channel`, `audience`, `subject`,
  `blocks`, `links`, `track_opens`; a campaign with a channel is a v0.18 campaign, without one it is the old kind and
  works as before (Follow-ups page). Both go through the same outbox (`scheduled_messages`) and job
  (`followups.send_due`); v0.18 rows are sent by `services/campaigns.deliver`, which drips use too.
- **The rules, in order, on every campaign/drip message:** marketing consent (and not unsubscribed) → polite hours
  (the country's quiet hours, else 21:00–08:00 business time: it waits, it isn't dropped) → frequency cap (1 offer a
  day, 3 a week per customer, campaigns + marketing drips together; it waits up to 7 days, then is skipped) → the
  channel's account and the customer's address on it → `inbox.send` (which applies the same rules again plus STOP and
  the reply window) → outside WhatsApp's 24 h window only the approved template (`campaigns.template`: a Meta template
  name or a Twilio Content SID), with no template that customer is skipped ("needs_template").
- **A way out in every message:** email: an unsubscribe link in the HTML and the text part and the headers
  `List-Unsubscribe: <https://…/u/<token>>` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058). `GET /u/`
  only asks (link scanners never unsubscribe anyone), `POST /u/` does — the token is the key, no session. SMS and
  WhatsApp: "Reply STOP…" in the business's language (STOP is honoured on every channel as before).
- **Never twice:** UNIQUE(campaign, contact) and UNIQUE(enrollment, step) in the outbox; `send_due` now *claims* its rows
  (send_at pushed LEASE_MINUTES ahead inside the SKIP LOCKED transaction, put back when done), so a second worker can't
  pick them up while the first is sending — this also hardens the older follow-ups. Throttle: rows are spread over
  minutes when the campaign is dispatched (THROTTLE per channel per minute).
- **Numbers** come from the outbox rows: sent/skipped/failed; delivered/read from the message's provider status;
  opened (pixel `/o/<token>.gif`, only when the owner ticked it — off by default; WhatsApp "read" counts as opened);
  clicked (`/c/<token>/<n>` redirects only to an address the message itself carried, stored at scheduling); replied
  (an inbound message within 7 days); unsubscribed; bookings and payments by those customers within 7 days.
- **Email HTML** is made by us from blocks (heading, text, image by URL, button): one 600 px table column, inline styles,
  no scripts or web fonts, the business's colour; previewed in a sandboxed `srcdoc` iframe with `script-src 'none'`.
  "Send me a test" goes to the owner's own address through the business's own account, prefixed [TEST]; never a
  customer row. AI drafts use the business's own key and its style profile and are never sent by themselves.
- **Drips** (`sequences`, `sequence_enrollments`): a customer enters once ever (UNIQUE) — by trigger, by recipe/workflow
  action "add to sequence", or by hand; leaves on reply/booking/unsubscribe (unsubscribe always ends a marketing drip).
  "Service" drips (about the customer's own request) need no marketing consent and have no cap, but still stop on STOP.
- **Workflows** are a list of steps (JSON), where an `if` holds `yes`/`no` lists — not a free graph — so a phone shows
  them as a vertical list with branch columns, and a run always ends. A run's place is a pointer (`[2, "no", 0]`).
  Runs start inside the event's transaction (like recipes, one savepoint each) and run until a wait; the jobs round
  wakes waits (claimed with SKIP LOCKED), and starts time-based triggers (a date field each year after 09:00 local;
  no visit in N days once a month; no reply for N hours). Loop protection: never in the same workflow twice at once
  (when a workflow sets itself off it is written down as "loop"), the recipe rule (something a workflow/recipe did
  can't restart one that ran for the same customer in the last hour), a shared chain depth with recipes (3), and at
  most 60 steps per run. Actions reuse `recipes._do` (one implementation): customer messages become Valeria's
  question (or her 60-second undo when "ask first" is off), offers ("marketing") never go to someone without consent,
  `{review_link}` is the same ask for everyone at most once per 90 days (never gated on how happy they are).
  "Open as a workflow" converts a recipe (its "only if" becomes an if-step) and switches the recipe off.
- **Phone agent** (`voice_agents`, `calls`): on only when switched on AND the business has its own Twilio number (the
  SMS channel; calls already reach `/hooks/<id>/voice`) AND its own AI key; otherwise calls work as before (ring the
  owner, missed-call text-back). Every webhook is Twilio-signature-checked (403 unsigned). Facts: prices/hours/where
  come from `answers._facts` (what the owner entered), never from the AI; other questions go to the AI with the facts
  and must come back `known` AND every number in the answer must appear in the facts ("grounded"), else "someone will
  call you back" + a task + a Valeria notice. Booking uses `booking.slots/book` (same rules and lock as the booking
  page). Recording only when the owner chose it, announced first, started with Twilio's Recordings API.
  **Design note — a realtime engine later:** `voice_agent.Brain` (what to say: facts, booking, messages, callbacks;
  no audio, no Twilio) is separate from `GatherTransport` (Brain replies → TwiML `<Gather input="speech">`, one webhook
  per turn). A streaming transport would answer the call with `<Connect><Stream url="wss://…/voice/stream/<call>">`,
  pipe the audio to a realtime speech model (the business's own key: Gemini Live or OpenAI Realtime), and on each final
  transcript call `Brain.turn()` and speak the `Reply` back — the facts, grounding, booking and callback rules stay in
  one place. It needs a websocket endpoint and a provider adapter; nothing in the Brain changes.
- **Invoices** (`invoices`, `plans`, `subscriptions`): numbers per business under an advisory lock (never reused; a
  mistake is voided); tax like quotes (`money.price` with the pack); an accepted quote becomes its invoice once, with
  the very numbers signed. The pay link is `payments.create` with the business's own account; `payment.paid` marks the
  invoice paid (in `services.log`). The providers we wire take one-off payments (Stripe Checkout `mode=payment`, Mercado
  Pago preferences, PayMongo links, Paystack, Razorpay links, mobile money, bank transfer), so a subscription is a
  recurring invoice + pay link each period (UNIQUE(subscription, period_start)), not the provider's own recurring
  billing. Overdue reminders are Valeria's `pay_reminder` question (money always asks). The customer page `/inv/<token>`
  wears the business's look; "PDF" is the browser's print-to-PDF (no PDF library on the server). Not a tax invoice
  where the country needs one (CFDI etc.): the page says so.
- **Ready setups** (`packs/snapshots/*.yaml`, `snapshot_applies`): bundles whose texts are locale keys (`sn_…`, said in
  the business's language). Apply merges by name and never overwrites (hours, website words and stage names only where
  empty); the preview IS the apply inside a savepoint that is rolled back. Undo (24 h) removes only what is still as the
  setup left it (a field with answers, a form with submissions, a workflow that ran… stay). Export: fields, recipes,
  workflows, sequences, campaign drafts, forms, replies, services, hours, the site's headline/about — references by
  name, team members dropped, never contacts, messages, deals, payments or notes. "Copy to another business" requires
  the same person to be its owner. Saved replies (`saved_replies`) are new: one tap in the inbox fills the reply box.
- **The app:** `/manifest.webmanifest`, `/sw.js` (served from the root, `no-store`, versioned cache `vo-shell-<version>`;
  caches only `/static/…` and `/offline`; pages always come from the network, offline → the offline page; never an
  `/app`, `/v1` or `/hooks` answer), `static/pwa.js` (install prompt; iPhone: how to add it), Web Push with this
  server's VAPID keys (`instance_kv`, private key encrypted) and RFC 8291 encryption in `services/push.py` (no
  Firebase): Valeria's questions and notices reach the owner's devices. `/.well-known/assetlinks.json` from
  `VO_ANDROID_PACKAGE` + `VO_ANDROID_SHA256` (404 when unset). Store steps: `docs/APP_STORES.md`.
- **Menu:** Campaigns and Workflows ride with the Follow-ups block (owner); Invoices with Payments (whole team). The
  phone agent, saved replies, notifications and ready setups are under Settings › More tools and on Start.
