# Valeria One — single-container (all-in-one) image

For platforms that deploy **one unmodified image behind one HTTP port** (for example
[PikaPods](https://www.pikapods.com/)). The app, its background jobs and PostgreSQL all run inside this one
image. For a normal server install use the multi-container `docker-compose.yml` in the repo root instead.

- **Image:** `ghcr.io/valeriaone/valeria-one-aio:latest` (multi-arch: amd64 + arm64)
- **Port:** `8000` (HTTP; terminate TLS at the platform/reverse proxy)
- **Persistent data:** everything (the PostgreSQL data dir and a generated secret key) lives under **`/data`** —
  mount a volume there.

## Run

```sh
docker run -d --name valeria-one -p 8000:8000 -v vo_data:/data \
  -e VO_PUBLIC_URL=https://your-host.example \
  ghcr.io/valeriaone/valeria-one-aio:latest
```

On first boot the container generates and persists a secret key and database password, initialises the
database, runs migrations, and prints a one-time **setup link** in its logs:

```
http://your-host/setup?k=…
^ open this link once to create your admin account
```

Open it once to create the first (admin) account. After that, login is required (`VO_SIGNUP=closed` by
default — only the first account and invited users). Updates: pull a newer image and restart; migrations run
automatically and `/data` is preserved.

## Optional environment

Everything is optional — it runs fine with none of it. AI, messaging and payments use the operator's/business's
**own** provider accounts (see the root `.env.example` and `docs/`).

| Variable | Meaning |
|---|---|
| `VO_PUBLIC_URL` | The public URL, used in links (setup link, emails). |
| `VO_SECRET_KEY` | Set to pin the key (otherwise generated and persisted on first boot). |
| `VO_DB_PASSWORD` | Set to pin the internal DB password (otherwise generated and persisted). |
| `VO_SIGNUP` | `closed` (default) or `open`. |

Health check: `GET /health` → `{"ok": true, "version": "…"}`.
