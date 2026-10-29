# Turning on the TikTok inbox

Businesses connect their **own** TikTok Business Account (Connect › TikTok, or `/app/tiktok`), and the direct
messages customers send them arrive in the normal inbox, with customers, AI auto-reply and everything else.
Until the steps below are done it stays off and the screen says "Not connected yet".

## What each business needs (shown on its connect screen)

- A **TikTok Business Account** (not a personal account) **linked to TikTok for Business**, set to accept
  direct messages from everyone.
- To be outside the **EEA, Switzerland and the UK**: TikTok does not offer Business Messaging there. Businesses
  in those countries see a notice instead of the button.

## TikTok's rules (Valeria One applies them)

- The **customer writes first**: a business cannot start a TikTok conversation.
- Answers only within **48 hours** of the customer's last message. There are no templates to go beyond that; a
  send outside it is blocked with a clear reason ("wait for them to write again").
- Win-backs and campaigns never pick TikTok (a quiet customer is always outside the window).
- The access token lasts **24 hours** and the refresh token **30 days**. Valeria One renews the access token
  before a send and the `tiktok` job renews every token with less than 2 hours left (so a quiet account keeps
  its refresh token alive). The refresh token is stored encrypted. If TikTok refuses, the business sees
  "Reconnect TikTok" on Today and on the TikTok page.

## 1. Developer app (the operator, once)

1. At business-api.tiktok.com (TikTok API for Business) create a developer account and an app.
2. Ask for the **Business Messaging API** and the scopes `message.list.read`, `message.list.send`,
   `message.list.manage` (plus `user.info.basic`). TikTok reviews this; it can take days.
3. Redirect URL: `https://YOUR-SERVER/app/tiktok/callback`.
4. Note the **App ID** and **Secret**.

## 2. Webhook (one for every business)

Register the server's one callback for direct-message events with TikTok (once, and again if the domain
changes):

```bash
curl -X POST https://business-api.tiktok.com/open_api/v1.3/business/webhook/update/ \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"YOUR_APP_ID","secret":"YOUR_SECRET","event_type":"DIRECT_MESSAGE",
       "callback_url":"https://YOUR-SERVER/hooks/tiktok"}'
```

Check it with `GET …/business/webhook/list/?app_id=…&secret=…&event_type=DIRECT_MESSAGE`.

TikTok signs each event with the header `TikTok-Signature: t=<unix time>,s=<hex HMAC-SHA256 of "<t>.<body>"
keyed with the app secret>`. Valeria One refuses anything unsigned, wrongly signed, or older than 5 minutes,
and routes the rest by the account's id (`user_openid`) to the business it belongs to. (The scheme is TikTok's
documented webhook signature; it is isolated in `channels/tiktok.py:check_signature` should TikTok change it.)

## 3. Server settings

```
VO_TIKTOK_APP_ID=…
VO_TIKTOK_APP_SECRET=…
```

Restart the server. The connect screen now shows **Sign in with TikTok**.
