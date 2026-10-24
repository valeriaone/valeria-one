# Partner API — for hosts and agencies

Run Valeria One for your customers from your own control panel and billing: create a business when a
customer signs up, pause it when a bill is unpaid, resume it when they pay, read usage for invoices,
delete it when they leave, and keep the server backed up and up to date.

Everything below is also on the server's own API reference at `/docs` (section **partner**).

## The key

A partner key runs the whole server, so keep it like a root password.

- In the app: **Server** page → **Partner keys** → *New key* (shown once), or
- On the server: `sudo valeria-one partner-key "Acme Hosting"`

Send it as `Authorization: Bearer vo_partner_…`. Keys can be revoked on the Server page; every action is
on record there (**Recent activity**) and in the business's own history. There is no partner API on the
public sandbox.

```bash
export VO=https://one.example.com          # your Valeria One server
export KEY=vo_partner_…
```

## Create a business for a customer

```bash
curl -s $VO/v1/partner/businesses -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "name": "Mama Njeri Salon", "country": "KE", "industry": "salon",
  "owner_email": "njeri@example.com", "partner_ref": "cust-1042",
  "blocks": ["calendar", "payments", "followups"]
}'
```

```json
{"business": {"id": "…", "name": "Mama Njeri Salon", "currency": "KES", "partner_ref": "cust-1042",
              "suspended": false, "people": 0, "blocks": {"calendar": true, "pipeline": false, …}},
 "invite_url": "https://one.example.com/join/…"}
```

Send the customer `invite_url` (it works once, for 7 days). `partner_ref` is your own id for the customer
(unique); `country` is a country pack (`GET /v1/packs/countries`); `blocks` is optional (default: all on).
Lost invitation: `POST /v1/partner/businesses/{id}/invite` with `{"email": "…", "role": "owner"}`.

## Find businesses

```bash
curl -s "$VO/v1/partner/businesses?partner_ref=cust-1042" -H "Authorization: Bearer $KEY"
curl -s  $VO/v1/partner/businesses/{id}                  -H "Authorization: Bearer $KEY"
```

## Pause and resume

```bash
curl -s -X POST $VO/v1/partner/businesses/{id}/suspend -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"reason": "invoice 1042 unpaid"}'
curl -s -X POST $VO/v1/partner/businesses/{id}/resume  -H "Authorization: Bearer $KEY"
```

Paused: nobody signs in (they see that their provider paused it), the business's own API keys answer 403,
nothing is sent (no replies, reminders, campaigns, AI) and its public pages are off. **Messages from its
customers still arrive and are kept**, so resuming loses nothing.

## Usage, for billing

```bash
curl -s "$VO/v1/partner/usage?month=2026-10" -H "Authorization: Bearer $KEY"
```

Per business, for the month (UTC): `messages_in`, `messages_out`, `customers`, `people`, `appointments`,
`payments_paid`, with your `partner_ref`. Counts only, never content. `&business_id=…` for one business.

## Delete for good

```bash
curl -s -X POST $VO/v1/partner/businesses/{id}/delete -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"confirm": "Mama Njeri Salon"}'
```

`confirm` must be the exact name. All the business's data goes; people who belonged to no other business
lose their account. Older backups still hold it until they rotate out (14 nights by default).

## The server

```bash
curl -s $VO/v1/partner/server -H "Authorization: Bearer $KEY"                 # version, newest release, jobs, disk, backups
curl -s -X POST $VO/v1/partner/server/backup -H "Authorization: Bearer $KEY"   # 202: runs within a minute
curl -s -X POST $VO/v1/partner/server/update -H "Authorization: Bearer $KEY"   # backup first, rolls back by itself if it fails
```

Backups and updates need the standard install (`deploy/install.sh`); otherwise they answer 409.

## One server per customer, or many customers per server

Both work. **Many per server**: one Valeria One, your customers as businesses on it (everything above).
**One per customer**: create a server with your cloud's own API and our installer as its start-up script
([`deploy/marketplace/cloud-init.yaml`](../deploy/marketplace/cloud-init.yaml)), then manage each one
with its partner key.

## Errors

JSON: `{"error": {"code": "invalid", "message": "…", "field": "partner_ref"}}`. 401 `unauthorized` (unknown
key) · 403 `suspended` (a paused business's own key) · 404 `not_found` · 409 `not_managed` · 422 `invalid`
(a request body of the wrong shape gets FastAPI's field-by-field `detail` list). Questions: tad.pascua@valai.mx
