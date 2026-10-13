#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $(id -u) != 0 ]]; then
    echo "Run this installer with sudo on the EC2 server." >&2
    exit 1
fi
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_root=/opt/cotinga
repository=${1:-ghcr.io/khoshru/cotinga}
if [[ ! -f "$app_root/.env" || ! -f "$app_root/image.env" ]]; then
    echo "Complete the first deployment before enabling automatic updates." >&2
    exit 1
fi
docker compose version >/dev/null
python3 "$source_dir/auto_update.py" --repository "$repository" --check

# Copy only updater code. The current app, .env, and database stay in place.
for file in auto_update.py deploy.sh; do
    temporary=$(mktemp "$app_root/.${file}.XXXXXX")
    install -m 0755 "$source_dir/$file" "$temporary"
    mv "$temporary" "$app_root/$file"
done
install -d -m 0700 "$app_root/docker-config"
temporary=$(mktemp "$app_root/.auto.env.XXXXXX")
printf 'COTINGA_IMAGE_REPOSITORY=%s\n' "$repository" > "$temporary"
mv "$temporary" "$app_root/auto.env"
install -m 0644 "$source_dir/cotinga-update.service" "$source_dir/cotinga-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cotinga-update.timer
systemctl start cotinga-update.service
echo "Automatic deployment enabled. Tested main releases are checked every minute."
