# Connect other apps — webhooks out, customers in, API keys

Everything here is on one screen in the app: **Connect other apps** (`/app/connect-apps`, owners only). It works with
Zapier, Make, n8n, Pipedream, your own website or any program that can send or receive an HTTPS request. Nothing here
costs anything and nothing goes through anyone else's account: the business connects its own apps.

## 1. Webhooks out — "tell my other app when something happens"

Give an `https://` address and pick when to send (none picked = everything). Each time one of those things happens in
the business, Valeria One sends one `POST` with a JSON body:

```json
{"id": 4182, "type": "contact.created", "business_id": "…", "subject_id": "…", "data": {…}, "created_at": "2026-10-30T18:02:11+00:00"}
```

| What you pick on screen      | `type`                |
|------------------------------|-----------------------|
| a new customer               | `contact.created`     |
| a new deal                   | `deal.created`        |
| a deal moves                 | `deal.stage_changed`  |
| an invoice is paid           | `invoice.paid`        |
| a payment arrives            | `payment.paid`        |
| a form is sent               | `form.submitted`      |
| a sale at the register       | `pos.sale`            |
| a booking online             | `booking.made`        |
| an appointment is booked     | `appointment.booked`  |
| a new member                 | `member.joined`       |

The API (`POST /v1/webhooks`) also takes any other event type, `"<kind>.*"` (e.g. `"deal.*"`) or `"*"`.

**Headers** on every delivery:

- `X-VO-Event` — the type (`contact.created`)
- `X-VO-Delivery` — this delivery's number (the same number if it is sent again)
- `X-VO-Signature` — hex HMAC-SHA256 of the exact body bytes, with the endpoint's signing secret (`whsec_…`, shown
  once when the webhook is made)

**Check the signature** before trusting a delivery (compare in constant time):

```python
import hashlib, hmac
def genuine(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), signature)
```

```js
const crypto = require("crypto");
const genuine = (secret, rawBody, sig) =>
  crypto.timingSafeEqual(Buffer.from(crypto.createHmac("sha256", secret).update(rawBody).digest("hex")), Buffer.from(sig));
```

**Answer** with any `2xx` within 10 seconds. Anything else (or no answer) is tried again by the jobs worker after
1 min, 5 min, 30 min, 2 h, 6 h, 12 h and 24 h; after that it is marked "gave up". The screen shows every delivery,
its answer and its error; "Send again" re-queues one that gave up. Redirects are not followed.

**Safety:** only `https://` addresses; never one that points to a private, local, loopback or link-local network
(`10.x`, `192.168.x`, `127.x`, `169.254.x`, `::1`, `fc00::/7`…). The address is checked when it is saved and again
before every delivery (DNS can change). An event is queued in the same database transaction as the thing that
happened: nothing is lost, and nothing is announced that didn't happen.

## 2. Customers in — "my other app sends a new customer here"

Make an address (pick what it makes: a customer, or a customer and a deal in a stage; optionally a tag). It looks like
`https://<your server>/hooks/in/vin_…` and is shown once — keep it like a password (only its fingerprint is stored;
turn it off any time).

Send `POST` with JSON (`Content-Type: application/json`) or a normal form:

| field                         | meaning                                                         |
|-------------------------------|-----------------------------------------------------------------|
| `name` (or `full_name`, `first_name`) | the customer's name                                     |
| `email`                       | their email                                                     |
| `phone` (or `phone_number`, `mobile`) | their phone, local or international                     |
| `message` (or `note`, `notes`, `comment`) | lands in the Inbox with the customer                |
| `tags`                        | comma-separated tags to add                                     |
| `deal` (or `title`)           | the deal's title (when the address makes deals)                 |
| `amount_minor`                | the deal's amount, an integer in the smallest unit (cents)      |

A name and a phone or an email are needed. The customer is found by phone or email first, so the same person is never
made twice. Answers: `201 {"ok": true, "contact_id": …, "deal_id": …}`, `422 {"error", "field"}` when something is
missing, `404` for an unknown or turned-off address, `413` for a body over 32 KB, `429` when called too often.

Marketing consent is **never** set from outside: who may get offers is decided by the business's own consent rules.

```bash
curl -X POST https://one.example/hooks/in/vin_xxxxxxxx -H 'Content-Type: application/json' \
  -d '{"name": "Ana López", "phone": "+52 55 1111 0001", "message": "Wants a quote for Saturday"}'
```

## 3. API keys

For your own programs: `Authorization: Bearer <key>` on the `/v1/…` API (OpenAPI at `/docs` on your server). A key is
shown once; only its fingerprint is kept; revoke it any time. The same webhooks can be managed by API:
`GET/POST /v1/webhooks`, `DELETE /v1/webhooks/{id}`, `GET /v1/webhooks/{id}/deliveries`, `POST /v1/webhooks/{id}/test`.

## Zapier / Make in two minutes

- **Valeria One → other app:** in Zapier choose "Webhooks by Zapier → Catch Hook" (Make: "Custom webhook"), copy the
  address it gives you into *Send to other apps*, pick the events, tap **Send a test**.
- **Other app → Valeria One:** in Zapier add "Webhooks by Zapier → POST" (Make: "HTTP → Make a request"), paste your
  *Customers coming in* address, send `name`, `email`, `phone`, `message` as JSON.
