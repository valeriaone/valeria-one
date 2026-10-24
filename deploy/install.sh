#!/usr/bin/env bash
# Install Valeria One on a fresh Ubuntu or Debian server (1 CPU, 2 GB RAM is plenty to start).
#
#   curl -fsSL https://raw.githubusercontent.com/valeriaone/valeria-one/main/deploy/install.sh | sudo bash
#   … | sudo bash -s -- --domain example.com        # your own domain, with HTTPS (point it here first)
#
# Options:
#   --domain DOMAIN     serve on DOMAIN with an automatic HTTPS certificate
#   --signup closed|open  closed (default): only the first account and people invited; open: anyone can sign up
#   --ref REF           a release tag (default: the newest release) or a branch
#   --repo URL          where to get the code (default: the official repository)
#   --root DIR          where it lives (default: /opt/valeria-one)
#   --http-port N --https-port N --bind ADDR   if 80/443 are taken (default 80, 443, all addresses)
#   --name NAME         Docker project name (default: valeria-one)
#   --no-systemd        no nightly backups / Server-page watcher (for tests and special setups)
#   --image             build a one-click image: install everything but generate no secrets; each new server
#                       makes its own on first boot
#
# Copyright (C) 2026 Tadewan Pascua. AGPL-3.0. "Valeria One" is a trademark of Tadewan Pascua.
set -euo pipefail

REPO=https://github.com/valeriaone/valeria-one.git
REF=""
ROOT=/opt/valeria-one
DOMAIN=""
SIGNUP=closed
HTTP_PORT=80
HTTPS_PORT=443
BIND=0.0.0.0
NAME=valeria-one
SYSTEMD=1
IMAGE=0

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN=${2:-}; shift 2 ;;
    --signup) SIGNUP=${2:-}; shift 2 ;;
    --ref) REF=${2:-}; shift 2 ;;
    --repo) REPO=${2:-}; shift 2 ;;
    --root) ROOT=${2:-}; shift 2 ;;
    --http-port) HTTP_PORT=${2:-}; shift 2 ;;
    --https-port) HTTPS_PORT=${2:-}; shift 2 ;;
    --bind) BIND=${2:-}; shift 2 ;;
    --name) NAME=${2:-}; shift 2 ;;
    --no-systemd) SYSTEMD=0; shift ;;
    --image) IMAGE=1; shift ;;
    --yes|-y) shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

case "$SIGNUP" in open|closed) ;; *) die "--signup is open or closed" ;; esac
[[ "$HTTP_PORT" =~ ^[0-9]+$ && "$HTTPS_PORT" =~ ^[0-9]+$ ]] || die "ports are numbers"
[[ "$NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "--name: lowercase letters, digits, - and _"
[ -z "$DOMAIN" ] || [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ && "$DOMAIN" == *.* ]] || die "--domain looks wrong: $DOMAIN"
if [ "$SYSTEMD" = 1 ] || [ "$IMAGE" = 1 ]; then [ "$(id -u)" = 0 ] || die "run as root (sudo)"; fi

# ---------------------------------------------------------------- tools
need_pkgs=()
for t in git curl openssl flock python3; do command -v "$t" >/dev/null || need_pkgs+=("$t"); done
if [ ${#need_pkgs[@]} -gt 0 ]; then
  [ "$(id -u)" = 0 ] || die "missing: ${need_pkgs[*]} (run as root to install them)"
  say "Installing ${need_pkgs[*]}"
  pk=()
  for t in "${need_pkgs[@]}"; do case "$t" in flock) pk+=(util-linux) ;; *) pk+=("$t") ;; esac; done
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates "${pk[@]}" >/dev/null
fi

install_docker() {
  [ "$(id -u)" = 0 ] || die "Docker is missing (run as root to install it)"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in ubuntu|debian) ;; *) die "install Docker with the compose plugin for your system, then run this again" ;; esac
  say "Installing Docker"
  install -m 0755 -d /etc/apt/keyrings
  if curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc \
     && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${VERSION_CODENAME:-} stable" > /etc/apt/sources.list.d/docker.list \
     && apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null; then
    :
  else                                                   # a release Docker does not package yet: the system's own
    rm -f /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null || die "could not install Docker"
  fi
  systemctl enable --now docker >/dev/null
}
docker compose version >/dev/null 2>&1 || install_docker
docker info >/dev/null 2>&1 || die "Docker is installed but not reachable (is it running? are you root or in the docker group?)"

# ---------------------------------------------------------------- the code
APP=$ROOT/app
mkdir -p "$ROOT"
if [ -d "$APP/.git" ]; then
  say "Updating the code in $APP"
  git -C "$APP" fetch --quiet --tags --force origin
else
  say "Getting Valeria One"
  git clone --quiet "$REPO" "$APP"
fi
if [ -z "$REF" ]; then
  REF=$(git -C "$APP" tag -l 'v[0-9]*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)
  REF=${REF:-main}
fi
if git -C "$APP" rev-parse -q --verify "refs/tags/$REF" >/dev/null; then
  git -C "$APP" -c advice.detachedHead=false checkout --quiet --force "refs/tags/$REF"
elif git -C "$APP" rev-parse -q --verify "refs/remotes/origin/$REF" >/dev/null; then
  git -C "$APP" -c advice.detachedHead=false checkout --quiet --force "refs/remotes/origin/$REF"
else
  git -C "$APP" -c advice.detachedHead=false checkout --quiet --force "$REF"
fi
say "Version $(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP/valeria_one/__init__.py") ($REF)"
chmod +x "$APP/deploy/valeria-one"
CLI=$APP/deploy/valeria-one

# ---------------------------------------------------------------- the command, the timers
if [ "$(id -u)" = 0 ]; then
  ln -sf "$CLI" /usr/local/bin/valeria-one
fi
if [ "$SYSTEMD" = 1 ]; then
  for u in valeria-one-daily.service valeria-one-daily.timer valeria-one-ops.path valeria-one-ops.service valeria-one-firstboot.service; do
    sed "s|@ROOT@|$ROOT|g" "$APP/deploy/systemd/$u" > "/etc/systemd/system/$u"
  done
  systemctl daemon-reload
  systemctl enable --quiet valeria-one-daily.timer valeria-one-ops.path
  [ "$IMAGE" = 0 ] || systemctl enable --quiet valeria-one-firstboot.service
fi

# ---------------------------------------------------------------- a one-click image: build, but no secrets
if [ "$IMAGE" = 1 ]; then
  say "Building the image (no secrets: every server makes its own on first boot)"
  printf 'VO_DB_PASSWORD=build-only\nVO_SECRET_KEY=build-only\nCOMPOSE_PROJECT_NAME=%s\n' "$NAME" > "$APP/.env"
  docker compose --project-directory "$APP" -f "$APP/docker-compose.yml" build --quiet
  docker compose --project-directory "$APP" -f "$APP/docker-compose.yml" pull --quiet db caddy
  rm -f "$APP/.env"
  cat > /etc/update-motd.d/99-valeria-one <<'EOF'
#!/bin/sh
[ -x /usr/local/bin/valeria-one ] && /usr/local/bin/valeria-one motd
exit 0
EOF
  chmod +x /etc/update-motd.d/99-valeria-one
  if command -v ufw >/dev/null; then
    ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null
  fi
  say "Image ready. On first boot each server creates its own keys and starts."
  exit 0
fi

# ---------------------------------------------------------------- this server
VO_INIT_DOMAIN=$DOMAIN VO_INIT_SIGNUP=$SIGNUP VO_INIT_HTTP_PORT=$HTTP_PORT VO_INIT_HTTPS_PORT=$HTTPS_PORT \
  VO_INIT_BIND=$BIND VO_INIT_NAME=$NAME "$CLI" init
say "Building and starting (a few minutes the first time)"
docker compose --project-directory "$APP" -f "$APP/docker-compose.yml" build --quiet
"$CLI" start
[ "$SYSTEMD" = 0 ] || systemctl start valeria-one-daily.timer valeria-one-ops.path

LINK=$("$CLI" setup-link 2>/dev/null || true)
echo
say "Valeria One is running."
if [ -n "$LINK" ]; then
  echo "    Create the first account here (this link works once):"
  echo "    $LINK"
fi
[ -n "$DOMAIN" ] || echo "    Own domain + HTTPS later:  valeria-one domain example.com"
echo "    Everything else:            valeria-one help"
