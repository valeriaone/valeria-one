#!/bin/bash
# Valeria One — Akamai/Linode StackScript. Creates a ready Valeria One server; the setup link is in
# /root/valeria-one.txt and shows when you log in.
# <UDF name="domain" label="Your domain (optional; point it at this server's IP first)" default="" />
# <UDF name="signup" label="Who can sign up" oneof="closed,open" default="closed" />
set -euo pipefail
exec > >(tee -a /var/log/valeria-one-install.log) 2>&1
curl -fsSL https://raw.githubusercontent.com/valeriaone/valeria-one/main/deploy/install.sh -o /root/valeria-one-install.sh
args=(--signup "${SIGNUP:-closed}")
[ -z "${DOMAIN:-}" ] || args+=(--domain "$DOMAIN")
bash /root/valeria-one-install.sh "${args[@]}" | tee /root/valeria-one.txt
cat > /etc/update-motd.d/99-valeria-one <<'MOTD'
#!/bin/sh
[ -x /usr/local/bin/valeria-one ] && /usr/local/bin/valeria-one motd
exit 0
MOTD
chmod +x /etc/update-motd.d/99-valeria-one
