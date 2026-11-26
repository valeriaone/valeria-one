# Valeria One — app + worker single-container image (external database)

For platforms that deploy **one unmodified image behind one HTTP port** *and* provide a **managed PostgreSQL**
(for example [PikaPods](https://www.pikapods.com/)). The web app and the background jobs worker run together
in this one image; the database is **external** (you supply its connection string).

- Image: `ghcr.io/valeriaone/valeria-one-app:latest` (multi-arch: amd64 + arm64)
- Port: `8000` (HTTP; terminate TLS at the platform)
- Database: external PostgreSQL via `VO_DATABASE_URL` (**required**)

For a bundled database (one `docker run`, nothing external) use `deploy/singlecontainer` instead; for a normal
server install use the multi-container `docker-compose.yml` in the repo root.

## Run

```sh
docker run -d --name valeria-one -p 8000:8000 -v vo_data:/data \
  -e VO_DATABASE_URL="postgresql+psycopg://USER:PASS@DB_HOST:5432/DBNAME" \
  -e VO_PUBLIC_URL=https://your-host.example \
  ghcr.io/valeriaone/valeria-one-app:latest
```

On boot it waits for the database, runs migrations, and prints a one-time **setup link** in its logs:

```
http://your-host/setup?k=…
^ open this link once to create your admin account
```

Open it once to create the first (admin) account. Login is required afterwards (`VO_SIGNUP=closed` by default —
only the first account and invited users). Updates: pull a newer image and restart; migrations run on boot.

## Environment

| Variable | Required | Meaning |
|---|---|---|
| `VO_DATABASE_URL` | **yes** | PostgreSQL connection string, `postgresql+psycopg://user:pass@host:5432/dbname`. |
| `VO_SECRET_KEY` | recommended | Set a stable key, or it is generated and persisted under `/data` on first boot. Needed so sessions and encrypted data survive restarts. |
| `VO_PUBLIC_URL` | recommended | Public URL used in links (setup link, emails). |
| `VO_SIGNUP` | no | `closed` (default) or `open`. |

AI, messaging and payments are optional and use the business's **own** provider accounts. Health check:
`GET /health` → `{"ok": true, "version": "…"}`.
