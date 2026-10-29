# Turning on the one-click Meta connection

Valeria One connects businesses to Meta as a **Tech Provider**: the server operator owns one Meta app,
and each business connects its own WhatsApp number, Facebook Pages and Instagram accounts to it through
Meta's own pop-ups. Until the steps below are done the feature stays off and businesses can still connect
channels by hand (Channels → Connect by hand).

## 1. Meta business + app

1. At business.facebook.com, create (or use) the operator's business portfolio and **verify the business**.
2. At developers.facebook.com → My Apps → Create app → type **Business**, linked to that portfolio.
3. Add products: **WhatsApp**, **Messenger**, **Instagram**, **Facebook Login for Business**, **Webhooks**.

## 2. Webhook (one for every business)

App → Webhooks. For each object (**WhatsApp Business Account**, **Page**, **Instagram**):

- Callback URL: `https://YOUR-SERVER/hooks/meta`
- Verify token: the value you put in `VO_META_VERIFY_TOKEN` (any long random string)
- Fields: WhatsApp → `messages`; Page → `messages`, `messaging_postbacks`, `message_deliveries`,
  `message_reads`, `leadgen`, `feed`; Instagram → `messages`, `comments`

## 3. Two configurations

- **WhatsApp Embedded Signup**: WhatsApp → Embedded Signup (or Facebook Login for Business →
  Configurations → "WhatsApp Embedded Signup"). Copy its configuration id → `VO_META_WA_CONFIG_ID`.
- **Pages + Instagram + leads**: Facebook Login for Business → Configurations → new, token type
  *User access token*, permissions: `pages_show_list`, `pages_messaging`, `pages_manage_metadata`,
  `pages_read_engagement`, `instagram_basic`, `instagram_manage_messages`, `instagram_manage_comments`,
  `leads_retrieval`, `business_management`, and for **Posts** (v0.12) `pages_manage_posts` and
  `instagram_content_publish`. Copy its id → `VO_META_LOGIN_CONFIG_ID`.
- If your configuration existed before v0.12, **edit it and add the two posting permissions** (and request them
  in App Review). Businesses connected before then have to connect Facebook + Instagram once more to grant them:
  Valeria One shows them "Reconnect to allow posting" (Posts, Connect), and renews the same inbox in place.
- Add your domain under App settings → Basic → App domains, and under Facebook Login for Business →
  Settings → Allowed domains for the JavaScript SDK.

## 3b. "Continue with Facebook" on sign-up and sign-in

The same app signs people up: one tap, no form, and the business arrives filled in from its Page (name,
phone, email, address, hours) with Messenger + Instagram already in the inbox.

- Facebook Login for Business → Settings → **Valid OAuth Redirect URIs**: `https://YOUR-SERVER/auth/facebook/callback`
- Configuration: a third one, *User access token*, with `email` and `public_profile` plus the Pages permissions
  of step 3 → `VO_META_SIGNUP_CONFIG_ID`. Without it the Pages configuration (`VO_META_LOGIN_CONFIG_ID`) is used,
  and people type their email once.
- "Continue with Google" uses the Google OAuth client of Calendar (`VO_GOOGLE_CLIENT_ID`/`SECRET`): add
  `https://YOUR-SERVER/auth/google/callback` as an authorised redirect URI. Name and email only.

## 4. Server settings

```
VO_META_APP_ID=…
VO_META_APP_SECRET=…          # App settings → Basic → App secret
VO_META_VERIFY_TOKEN=…        # the same string as in step 2
VO_META_WA_CONFIG_ID=…
VO_META_LOGIN_CONFIG_ID=…
VO_META_SIGNUP_CONFIG_ID=…    # optional, step 3b
VO_GRAPH_VERSION=v23.0        # or the current Graph API version
```

Restart the server. Channels and Connect now show the one-tap buttons for WhatsApp and Facebook Page +
Instagram, and sign-up / sign-in show **Continue with Facebook**.

## 5. App review (to serve businesses other than your own)

Request **Advanced Access** for the permissions above (and `email`, `public_profile`) plus `whatsapp_business_messaging` and
`whatsapp_business_management`, and become a **Tech Provider** (WhatsApp → Solution providers). Meta asks
for short screen recordings: connecting WhatsApp, sending a message, creating a template, connecting a
Page and receiving a message and a lead. Every one of those can be recorded from this app.

## What happens when a business connects

- **WhatsApp**: the code from Meta's pop-up is exchanged for the business's token; our app is subscribed
  to its WhatsApp account; the number is registered for the Cloud API (with a random two-step PIN, stored
  encrypted); the number appears in the inbox.
- **Pages**: the business picks which Pages; each is subscribed for messages and Lead Ads; a linked
  Instagram professional account is added too.
- Tokens are encrypted at rest and never shown again. A number or Page can belong to one business only.
- Every event arrives at `/hooks/meta`, is checked against the app secret, and is routed to the business
  that owns that number, Page or Instagram account. Comments that match a business's rule get one private
  reply (Meta allows one, within 7 days). Lead Ads become customers (matched by phone or email
  when they already exist), tagged `lead-ad`, with every extra form answer kept in their notes.

## 5. Posts (Facebook Page + Instagram)

With the two permissions above, owners write a post (or let their AI write it), approve it and publish it now or
at a time they pick (Posts). What Valeria One calls, with the business's own Page token:

- Facebook Page: `POST /{page-id}/feed` (text) or `POST /{page-id}/photos` (picture + text).
- Instagram: `GET /{ig-user-id}/content_publishing_limit` first (the 24-hour quota), then `POST /{ig-user-id}/media`
  with a public JPEG `image_url` and the caption, then `POST /{ig-user-id}/media_publish`. Instagram has no
  text-only posts, so a picture is required.
- The picture is served by this server (`/pf/…`, signed, 3 days; or the website's own public file address), so
  `VO_PUBLIC_URL` must be the public HTTPS address Meta can reach.
- `VO_META_AI_LABEL=on` adds `is_ai_generated=true` to AI-written Instagram posts. It is off by default because
  not every Graph version accepts the field: switch it on only after checking it works with your
  `VO_GRAPH_VERSION`.
- Scheduling is done by the `posts` job (every minute, `python -m valeria_one.jobs all`), not by Meta's
  `scheduled_publish_time`, so one queue serves Facebook, Instagram and Google and a post can be changed or
  cancelled until it goes out.
