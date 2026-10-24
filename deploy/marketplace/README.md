# One-click Valeria One on cloud marketplaces

Every path ends the same way: a server running Valeria One that makes its own secrets, backs itself up
every night, and prints its one-time setup link when its owner logs in.

| Where | How | Files |
|---|---|---|
| DigitalOcean Marketplace | snapshot built with Packer, submitted in the Vendor Portal | `packer/` |
| Vultr Marketplace | snapshot built with Packer, submitted as a Vultr Marketplace app | `packer/` |
| Akamai / Linode | StackScript (public), or a Marketplace app | `linode-stackscript.sh` |
| Anything with "user data" (AWS, Azure, GCP, Hetzner, OVHcloud, Oracle…) | cloud-init | `cloud-init.yaml` |
| Any Ubuntu / Debian server | one command | `../install.sh` |

## Build the images

```bash
cd deploy/marketplace/packer
packer init .
DIGITALOCEAN_TOKEN=… packer build -only=digitalocean.valeria-one .
VULTR_API_KEY=…      packer build -only=vultr.valeria-one .
packer build -var ref=v0.2.0 …       # a specific release (default: the newest)
```

What the build does: Ubuntu 24.04 + updates → `install.sh --image` (Docker, the code, the built app, the
Postgres and Caddy images, the first-boot service, firewall 22/80/443, login message) → cleanup (no SSH
keys, logs or history; the build fails if a `.env` was left) → DigitalOcean's own image check.

On a new server's first boot, `valeria-one-firstboot.service` writes `/opt/valeria-one/app/.env` with a fresh
secret key and database password and the server's IP as its address, and starts everything. The login
message shows the address and the setup link until the first account exists.

## Listing text (short)

**Valeria One — the back office for any business on WhatsApp, Messenger and Instagram.** One inbox for every
channel, customers, pipeline, booking with no double-booking, a website, payments to the business's own
account, quotes with e-signature, reminders and campaigns. Any country, any trade, 11 languages. Works
without AI; add AI with your own key. Open source (AGPL-3.0).

**After you create the server:** log in by SSH and open the setup link shown, or run `valeria-one setup-link`.
For your domain with HTTPS: point it at the server and run `valeria-one domain example.com`.

**Recommended size:** 1 CPU / 2 GB. **Ports:** 22, 80, 443. **Support:** tad.pascua@valai.mx ·
https://github.com/valeriaone/valeria-one

## Before submitting

- Vendor accounts are opened by the publisher (identity and payout details).
- Test one server from each built image: create → setup link → sign in → Server page shows backups and
  "Up to date".
- The name "Valeria One" and its logo are trademarks of Tadewan Pascua (see `TRADEMARK.md`).
