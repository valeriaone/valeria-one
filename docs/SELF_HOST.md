# Run Valeria One on your own server

Your server, your data, your domain. It works on day one without AI; AI is a switch, with your own key.

## Install (5 minutes)

You need a fresh **Ubuntu 22.04+ or Debian 12+** server with 1 CPU and 2 GB of memory, and SSH access as root.

```bash
curl -fsSL https://raw.githubusercontent.com/valeriaone/valeria-one/main/deploy/install.sh | sudo bash
```

With your own domain (point its DNS `A` record at the server first), add `-s -- --domain example.com`.
HTTPS is set up and renewed by itself.

When it finishes it prints a **setup link**. Open it to create the first account: it runs the server and
owns its first business. The link works once; lost it? `sudo valeria-one setup-link`.

One-click images and scripts for cloud providers are in [`deploy/marketplace`](../deploy/marketplace/README.md).
A step-by-step first day, from install to your first message: [TUTORIAL.md](TUTORIAL.md).

### What gets installed

| Where | What |
|---|---|
| `/opt/valeria-one/app` | the code (a git checkout of the release) and `.env`, the server's private settings |
| `/opt/valeria-one/backups` | nightly backups, the newest 14 kept |
| `/opt/valeria-one/ops` | how the Server page asks for backups and updates, and where it reads the results |
| Docker | Postgres, the app, the background jobs, and Caddy (the only thing on ports 80/443) |
| systemd | `valeria-one-daily.timer` (backup + release check at night), `valeria-one-ops.path` (watches the Server page) |
| `/usr/local/bin/valeria-one` | the command below |

## Every day: the `valeria-one` command

```text
valeria-one status              what is running, version, address, last backup
valeria-one setup-link          the one-time link to create the first account
valeria-one domain example.com  serve on your own domain, with HTTPS
valeria-one backup              back up now
valeria-one restore FILE        put a backup back (a safety backup is made first)
valeria-one update              newest release: backup, update, roll back by itself if it fails
valeria-one logs [api|jobs|db|caddy]
valeria-one make-admin EMAIL    let an existing account run this server
```

The account that runs the server also has a **Server** page in the app: version and updates, background
jobs, disk, backups (download them), and **Add a business** for a client, with an owner invitation link.

## Backups

- Every night, and before every update and every restore.
- A backup is one file: the database, plus this server's `.env`. The `.env` holds the key that unlocks the
  secrets businesses saved (channel tokens, AI keys), so **a backup is as private as the server itself**.
- Keep copies somewhere else: download them from the Server page, or copy `/opt/valeria-one/backups` with
  your own tools (rsync, rclone, your provider's snapshots).
- `valeria-one restore FILE` works on the same server or a new one: the backup brings its data and keys; the
  new server keeps its own address, ports and database password. A backup from a newer version asks you to
  update first.

## Updates

`valeria-one update` (or **Update now** on the Server page):

1. makes a backup,
2. switches the code to the newest release and rebuilds,
3. starts it; the database is upgraded on start,
4. if the new version is not healthy within 5 minutes, puts the previous version **and the data from step 1**
   back by itself, and the Server page says so.

Releases are the `vX.Y.Z` tags of the official repository. Nothing updates without you asking.

## Who can sign up

`VO_SIGNUP=closed` (the default for installs): only the first account and the people invited (team
invitations, or businesses the admin adds). `VO_SIGNUP=open` lets anyone create a business: for running it
as a service. Change it in `/opt/valeria-one/app/.env`, then `valeria-one restart`.

## Email from the server

Password resets, billing notices and invitations go out through any SMTP account: set `VO_SMTP_HOST`,
`VO_SMTP_PORT` (587), `VO_SMTP_USER`, `VO_SMTP_PASSWORD`, `VO_SMTP_FROM` and `VO_SMTP_FROM_NAME`, then
`valeria-one restart`. Without it nothing is sent; give someone a password link yourself with `sudo valeria-one reset-link EMAIL`.

## Charging for it (running it as a service)

Self-hosted installs never charge anyone. To sell Valeria One as a hosted service, set `VO_BILLING=on`:

- New businesses get **30 days free**; then Free (inbox and customers, 1 person) unless they pay.
  Nothing is ever locked or deleted. Businesses that existed before you switched it on get the full trial.
- **Launch window:** `VO_LAUNCH_MONTHS=12` gives every business that signs up while it is set 12 free months
  instead (sign-up says so, and what it costs after). When those months end unpaid the business is *paused*
  (`VO_AFTER_PROMO=pause`, the default): nothing is sent, the screens show Billing, Settings and the data
  export, the API answers 402 except `GET /v1/business` and `GET /v1/export`; messages still arrive and
  nothing is deleted. `VO_AFTER_PROMO=free` sends them to Free instead. Owners are emailed 30 days before.
  Unsetting `VO_LAUNCH_MONTHS` closes the window for new sign-ups only.
- Sign-ups from countries under comprehensive sanctions (Cuba, Iran, North Korea, Syria) are refused;
  `VO_BLOCKED_COUNTRIES` (comma-separated ISO codes) changes the list.
- One plan, everything, the whole team, priced by the business's country: 39 USD, 249 MXN, 799 PHP or
  999 INR a month, USD everywhere else (`VO_PRICES` changes it, e.g.
  `{"starter": {"USD": 4900}}`, in minor units). `VO_SELLER_COUNTRY=MX` adds that country's tax (IVA) to
  domestic customers only.
- `VO_MARKETING=on` adds the public pages of a service: the home page (English) and /mx (Spanish), pages
  per trade and per competitor, missed-call text-back, Terms, Privacy and Texting terms (drafts), sitemap.
- **Bank transfer:** set where it goes on the Server page (Billing, asks your password again). Businesses
  send it with their reference; you confirm it on the Server page; they get an email.
- **Card:** set `VO_STRIPE_SECRET_KEY` and `VO_STRIPE_WEBHOOK_SECRET`, and point a Stripe webhook at
  `https://YOUR-DOMAIN/hooks/billing/stripe` (events `checkout.session.completed`, `invoice.paid`,
  `customer.subscription.deleted`). Subscriptions renew by themselves; "Change card or cancel" opens
  Stripe's own portal (turn it on in Stripe).
- **Referrals:** every business's public pages say "Made with Valeria One" with its referral code
  (`VO_BADGE=off` hides it). The Server page shows who brought whom and the commission
  (`VO_REFERRAL_PERCENT`, default 30).
- Mark a business **No charge** on the Server page (friends, partners). The server's own business never pays.

## Firewall and ports

Open 80 and 443 (and SSH). The app's own port is never published; only Caddy faces the internet. If 80/443
are taken, install with `--http-port` / `--https-port`.

## For hosting providers

Valeria One is built to be offered as a one-click app: the image carries no secrets (each server creates
its own on first boot), signs nothing up until its owner opens the setup link, backs itself up, and updates
with automatic rollback. Images: [`deploy/marketplace`](../deploy/marketplace/README.md).
Run it from your own control panel and billing with the [Partner API](PARTNER_API.md): create businesses for
your customers, pause and resume them, read usage, back up and update.

The code is AGPL-3.0. The name "Valeria One" is a trademark: offering it under that name, bundling it, or a
commercial licence is by agreement — tad.pascua@valai.mx. See [TRADEMARK.md](../TRADEMARK.md).
