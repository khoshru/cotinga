#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $(id -u) != 0 ]]; then
    echo "Run this setup with sudo on the EC2 server." >&2
    exit 1
fi
# This installer targets Ubuntu 24.04 on an x86_64 EC2 instance.
# shellcheck source=/dev/null
source /etc/os-release
if [[ "$ID" != ubuntu || $(uname -m) != x86_64 ]]; then
    echo "Use an Ubuntu x86_64 instance for this setup." >&2
    exit 1
fi
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
apt-get update
apt-get install -y ca-certificates curl python3 util-linux

if ! docker compose version >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

install -d -m 0750 /opt/cotinga /opt/cotinga/backups
install -d -o 10001 -g 10001 -m 0750 /opt/cotinga/data
install -m 0644 "$source_dir/compose.yaml" "$source_dir/Caddyfile" "$source_dir/backup_sqlite.py" /opt/cotinga/
install -m 0755 "$source_dir/deploy.sh" /opt/cotinga/deploy.sh
if [[ ! -f /opt/cotinga/.env ]]; then
    python3 - <<'PY'
from pathlib import Path
import secrets
values = {
    'COTINGA_DOMAIN': '',
    'COTINGA_TLS_EMAIL': '',
    'DJANGO_SECRET_KEY': secrets.token_urlsafe(64),
    'DJANGO_TIME_ZONE': 'Asia/Tbilisi',
    'COTINGA_OWNER_EMAIL': '',
    'COTINGA_OWNER_PASSWORD': '',
}
with Path('/opt/cotinga/.env').open('x') as stream:
    stream.write(''.join(f'{key}={value}\n' for key, value in values.items()))
PY
fi
echo "Setup complete. Edit /opt/cotinga/.env with your domain, TLS email, and optional owner account."
echo "SQLite data stays in /opt/cotinga/data; deployments leave this directory in place."
