#!/bin/bash
# Leave nothing of the build machine in the image: no keys, no logs, no history.
set -euo pipefail
apt-get -y autoremove
apt-get -y autoclean
rm -rf /tmp/* /var/tmp/*
history -c || true
cat /dev/null > /root/.bash_history
unset HISTFILE
find /var/log -mtime -1 -type f -exec truncate -s 0 {} \;
rm -rf /var/log/*.gz /var/log/*.[0-9] /var/log/*-????????
rm -rf /var/lib/cloud/instances/*
rm -f /root/.ssh/authorized_keys /etc/ssh/*key*
touch /etc/ssh/revoked_keys
chmod 600 /etc/ssh/revoked_keys
test ! -e /opt/valeria-one/app/.env                     # an image never carries secrets
dd if=/dev/zero of=/zerofile bs=4096 status=none || true
rm -f /zerofile
sync
