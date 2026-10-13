#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

source_dir=""
image_type=local
expected_revision=""
stage=""
snapshot=""
container=""
rollback_armed=false
caddy_changed=false
if [[ $# == 1 && -d "$1" ]]; then
    source_dir=$(realpath "$1")
    if [[ ! -f "$source_dir/Dockerfile" || ! -f "$source_dir/uv.lock" ]]; then
        echo "Provide the Cotinga source checkout directory." >&2
        exit 1
    fi
    revision=$(git -c safe.directory="$source_dir" -C "$source_dir" rev-parse --verify HEAD)
    image="cotinga:${revision}"
    expected_revision=$revision
elif [[ $# == 1 && "$1" =~ ^ghcr\.io/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9._-]*@sha256:[a-f0-9]{64}$ ]]; then
    image=$1
    image_type=ghcr
    expected_repository=${COTINGA_IMAGE_REPOSITORY:-ghcr.io/khoshru/cotinga}
    if [[ "${image%@*}" != "$expected_repository" ]]; then
        echo "Only the configured GitHub image repository may be deployed." >&2
        exit 1
    fi
else
    echo "Usage: deploy.sh SOURCE_DIRECTORY | GHCR_IMAGE_DIGEST" >&2
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
    exit 75
fi

compose() {
    docker compose --project-name cotinga --project-directory "$app_root" --env-file .env --env-file image.env -f compose.yaml "$@"
}

atomic_install() {
    local source=$1 destination=$2 mode=$3 temporary
    temporary=$(mktemp "${destination}.XXXXXX")
    install -m "$mode" "$source" "$temporary"
    mv "$temporary" "$destination"
}

cleanup() {
    if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi
    if [[ -n "$stage" ]]; then rm -rf "$stage"; fi
}

rollback() {
    local status=$?
    trap - ERR
    # Command substitutions inherit ERR; recovery belongs to the parent only.
    if (( BASH_SUBSHELL > 0 )); then exit "$status"; fi
    if [[ "$rollback_armed" == true ]]; then
        echo "Deployment failed; attempting the previous image. SQLite is left intact; its backup is in backups/." >&2
        compose stop --timeout 30 web || true
        for file in compose.yaml Caddyfile backup_sqlite.py deploy.sh auto_update.py image.env; do
            if [[ -f "$snapshot/$file" ]]; then
                local mode=0644
                if [[ "$file" == image.env ]]; then mode=0600; fi
                if [[ "$file" == deploy.sh ]]; then mode=0755; fi
                atomic_install "$snapshot/$file" "$app_root/$file" "$mode" || true
            fi
        done
        compose up --detach --remove-orphans --wait --wait-timeout 180 || true
        if [[ "$caddy_changed" == true ]]; then
            compose up --detach --no-deps --force-recreate --wait --wait-timeout 60 caddy || true
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap rollback ERR

# Build or pull before stopping the current app.
if [[ -n "$source_dir" ]]; then
    docker build --build-arg "COTINGA_REVISION=$revision" --tag "$image" "$source_dir"
    image=$(docker image inspect --format '{{.Id}}' "$image")
elif [[ "$image_type" == ghcr ]]; then
    if ! docker pull "$image"; then
        echo "Image download failed; the updater will retry without stopping the app." >&2
        exit 75
    fi
    labels=$(docker image inspect --format '{{json .Config.Labels}}' "$image")
    expected_source="https://github.com/${expected_repository#ghcr.io/}"
    expected_revision=$(printf '%s' "$labels" | python3 -c '
import json, re, sys
labels = json.load(sys.stdin)
if not isinstance(labels, dict):
    raise SystemExit("Image is missing repository and revision labels")
revision = labels.get("org.opencontainers.image.revision", "")
if labels.get("org.opencontainers.image.source") != sys.argv[1] or not isinstance(revision, str) or not re.fullmatch("[a-f0-9]{40}", revision):
    raise SystemExit("Image source or revision does not match the trusted repository")
print(revision)
' "$expected_source")
    stage=$(mktemp -d ./candidate.XXXXXX)
    container=$(docker create "$image")
    for file in compose.yaml Caddyfile backup_sqlite.py deploy.sh auto_update.py; do
        docker cp "$container:/app/deploy/ec2/$file" "$stage/$file"
        if [[ ! -f "$stage/$file" || -L "$stage/$file" ]]; then
            echo "The release is missing a regular deployment file: $file" >&2
            exit 1
        fi
    done
    docker rm "$container" >/dev/null
    container=""
    bash -n "$stage/deploy.sh"
    python3 -m py_compile "$stage/auto_update.py" "$stage/backup_sqlite.py"
    COTINGA_IMAGE="$image" docker compose --project-name cotinga --project-directory "$app_root" --env-file .env -f "$stage/compose.yaml" config --quiet
    if ! cmp -s "$stage/Caddyfile" Caddyfile; then caddy_changed=true; fi
fi

stamp=$(date -u +%Y%m%dT%H%M%S)-$$
snapshot="backups/host-${stamp}"
mkdir -m 0700 "$snapshot"
for file in compose.yaml Caddyfile backup_sqlite.py deploy.sh auto_update.py image.env; do
    if [[ -f "$file" ]]; then cp -p "$file" "$snapshot/$file"; fi
done
if [[ -f image.env ]]; then
    cp image.env "backups/image-${stamp}.env"
    rollback_armed=true
    compose stop --timeout 30 web
fi
if [[ -f data/db.sqlite3 ]]; then
    python3 backup_sqlite.py data/db.sqlite3 "backups/db-${stamp}.sqlite3"
fi

if [[ -n "$stage" ]]; then
    for file in compose.yaml Caddyfile backup_sqlite.py auto_update.py; do
        atomic_install "$stage/$file" "$app_root/$file" 0644
    done
    atomic_install "$stage/deploy.sh" "$app_root/deploy.sh" 0755
fi
release_file=$(mktemp ./image.env.XXXXXX)
printf 'COTINGA_IMAGE=%s\n' "$image" > "$release_file"
mv "$release_file" image.env
compose config --quiet

# One container uses this SQLite file. Replacement preserves the bind mount.
compose up --detach --remove-orphans --wait --wait-timeout 180
if [[ "$caddy_changed" == true ]]; then
    compose up --detach --no-deps --force-recreate --wait --wait-timeout 60 caddy
fi
domain=$(compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["caddy"]["environment"]["COTINGA_DOMAIN"])')
if [[ ! "$domain" =~ ^[a-zA-Z0-9.-]+$ ]]; then
    echo "Set a valid domain in .env." >&2
    false
fi
health=$(curl --fail --silent --show-error --retry 4 --retry-all-errors --retry-delay 3 --max-time 10 "https://${domain}/health/")
printf '%s' "$health" | python3 -c '
import json, sys
body = json.load(sys.stdin)
if body.get("status") != "ok" or (sys.argv[1] and body.get("revision") != sys.argv[1]):
    raise SystemExit("The deployed application did not pass its revision and health checks")
' "$expected_revision"
printf '\nDeployment healthy: https://%s\n' "$domain"
