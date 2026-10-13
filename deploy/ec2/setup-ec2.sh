#!/usr/bin/env bash
set -euo pipefail

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
apt-get install -y ca-certificates curl unzip python3 util-linux

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

if ! command -v aws >/dev/null 2>&1; then
    install_dir=$(mktemp -d)
    trap 'rm -rf "$install_dir"' EXIT
    curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o "$install_dir/awscliv2.zip"
    unzip -q "$install_dir/awscliv2.zip" -d "$install_dir"
    "$install_dir/aws/install"
fi

install -d -m 0750 /opt/cotinga /opt/cotinga/backups
install -d -o 10001 -g 10001 -m 0750 /opt/cotinga/data
install -m 0644 "$source_dir/compose.yaml" "$source_dir/Caddyfile" "$source_dir/backup_sqlite.py" /opt/cotinga/
install -m 0755 "$source_dir/deploy.sh" /opt/cotinga/deploy.sh
if [[ ! -f /opt/cotinga/.env ]]; then
    install -m 0600 "$source_dir/.env.example" /opt/cotinga/.env
    python3 - <<'PY'
from pathlib import Path
import secrets
path = Path('/opt/cotinga/.env')
path.write_text(path.read_text().replace('generated-by-setup', secrets.token_urlsafe(64)))
PY
fi
echo "Setup complete. Edit /opt/cotinga/.env with your domain, TLS email, and optional owner account."
echo "SQLite data stays in /opt/cotinga/data; deployments leave this directory in place."
