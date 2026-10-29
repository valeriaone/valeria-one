# Turning on Google Business Profile

Each business connects its **own** Google Business Profile (Connect › Google Business Profile, or
`/app/google`): its reviews arrive in Valeria One and the owner answers them there (the AI may draft an answer;
nothing is published without the owner's approval), posts go to the profile, and the last 30 days of calls,
website clicks, direction requests and views are shown. Until the steps below are done it stays off and the
screens say "Not connected yet".

The operator (whoever runs this server) does this once. It costs nothing, but Google's approval takes time:
start with step 4 early.

## 1. Google Cloud project

1. At console.cloud.google.com create a project (or use the one you made for Calendar).
2. APIs & Services → Library → **enable** each of these:
   - **My Business Account Management API** (accounts)
   - **My Business Business Information API** (the profile, its hours and phone)
   - **Google My Business API** (the v4 API: reviews, review replies and local posts)
   - **Business Profile Performance API** (insights)

## 2. OAuth consent screen — Testing mode with test users

1. APIs & Services → OAuth consent screen → User type **External** → app name, support email, your domain.
2. Scopes: add `https://www.googleapis.com/auth/business.manage` (plus `openid` and `email`).
3. Publishing status: **Testing**. Add every owner who will connect as a **test user** (their Google email),
   up to 100.
4. Testing mode has a cost: Google ends the access every **7 days** (the refresh token expires). Valeria One
   reminds the owner on day 6 and, when Google ends it, shows "Reconnect Google" on Today and on the Google page
   and emails the owners once. To stop the weekly reconnect, publish the app and pass Google's verification of
   the `business.manage` scope, then set `VO_GOOGLE_OAUTH_TESTING=off`.

## 3. OAuth client

APIs & Services → Credentials → Create credentials → OAuth client ID → **Web application**. Authorised
redirect URIs:

- `https://YOUR-SERVER/app/google/callback` (Business Profile)
- `https://YOUR-SERVER/app/calendar/google/callback` and `https://YOUR-SERVER/auth/google/callback` (Calendar
  and "Continue with Google", if you use them — the same client serves all three)

## 4. Basic API Access (the quota starts at 0)

The Business Profile APIs start with a quota of **0 requests a minute**: nothing works until Google approves
the project. Apply with the **GBP API access request form** (Google Business Profile APIs → "Request access").
What the form needs, from experience:

- A **verified** Business Profile that has existed for **60 days or more**, with a **website** on it.
- Submit it **from an email address that is an owner or manager of that profile** (the operator's own business
  is fine), and give the Cloud project number.
- Describe the use honestly: "a CRM where each business connects its own profile to read and answer its reviews,
  publish posts and see its insights".

Approval usually takes days; the answer arrives by email. Until then, connecting works but every call fails
with a quota error (shown calmly on the page).

## 5. Server settings

```
VO_GOOGLE_CLIENT_ID=…apps.googleusercontent.com
VO_GOOGLE_CLIENT_SECRET=…
VO_GOOGLE_BUSINESS=on          # switches Business Profile on (Calendar alone doesn't need it)
VO_GOOGLE_OAUTH_TESTING=on     # default; off once the app is published and verified
```

Restart the server. The `google` job (part of `python -m valeria_one.jobs all`, every minute) reads each
profile's reviews at most every 30 minutes, its info and insights once a day, and stores each review once.

## What Valeria One keeps to (Google's rules)

- At most **10 changes a minute** per profile (review answers, posts); after a **429** it waits (Google's
  `Retry-After`, else 5 minutes) before asking Google anything for that profile again.
- Review answers up to **4096 bytes**; post pictures JPG/PNG up to 5 MB, at a public address.
- Never review gating (the same review request goes to every customer), never an incentive for a review, never
  an AI-written review, and never a referral reward in the same message as a review request.
- The refresh token is stored encrypted (`VO_SECRET_KEY`); disconnecting forgets it.
