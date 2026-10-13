#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 2 ]]; then
    echo "Usage: deploy.sh ECR_IMAGE_DIGEST AWS_REGION" >&2
    exit 1
fi
image=$1
region=$2
if [[ ! "$image" =~ ^[0-9]{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || [[ "${BASH_REMATCH[1]}" != "$region" ]]; then
    echo "Provide an ECR SHA-256 image digest in the specified AWS region." >&2
    exit 1
fi

app_root=${COTINGA_ROOT:-/opt/cotinga}
export COTINGA_ROOT="$app_root"
cd "$app_root"
if [[ ! -f .env || ! -d data || ! -d backups ]]; then
    echo "Run setup-ec2.sh and configure /opt/cotinga/.env first." >&2
    exit 1
fi
exec 9>deploy.lock
if ! flock -n 9; then
    echo "A deployment is already running." >&2
    exit 1
fi

compose() {
    docker compose --project-name cotinga --env-file .env --env-file image.env -f compose.yaml "$@"
}

# Pull before stopping the current app. Credentials come from the EC2 IAM role.
registry=${image%%/*}
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
docker pull "$image"

stamp=$(date -u +%Y%m%dT%H%M%S)-$$
if [[ -f image.env ]]; then
    cp image.env "backups/image-${stamp}.env"
    compose stop --timeout 30 web
fi
if [[ -f data/db.sqlite3 ]]; then
    python3 backup_sqlite.py data/db.sqlite3 "backups/db-${stamp}.sqlite3"
fi

release_file=$(mktemp ./image.env.XXXXXX)
printf 'COTINGA_IMAGE=%s\n' "$image" > "$release_file"
mv "$release_file" image.env
compose config --quiet

# One container uses this SQLite file. Replacement preserves the bind mount.
compose up --detach --remove-orphans --wait --wait-timeout 180
domain=$(compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["caddy"]["environment"]["COTINGA_DOMAIN"])')
if [[ ! "$domain" =~ ^[a-zA-Z0-9.-]+$ ]]; then
    echo "Set a valid domain in .env." >&2
    exit 1
fi
curl --fail --silent --show-error --retry 4 --retry-all-errors --retry-delay 3 --max-time 10 "https://${domain}/health/"
printf '\nDeployment healthy: https://%s\n' "$domain"
