"""Apply the production image published by this repository's CI workflow."""

import argparse
import fcntl
import json
import os
import re

# Deployment uses an argument list and a fixed interpreter, never a shell string.
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_REPOSITORY = "ghcr.io/khoshru/cotinga"
REPOSITORY_PATTERN = re.compile(r"ghcr\.io/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9._-]*")
DIGEST_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class UpdateError(Exception):
    pass


def validate_repository(repository: str) -> str:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise UpdateError(
            "Use a GitHub Container Registry repository: ghcr.io/OWNER/NAME"
        )
    return repository


def production_image(repository: str) -> str:
    validate_repository(repository)
    scope = repository.removeprefix("ghcr.io/")
    query = urlencode({"service": "ghcr.io", "scope": f"repository:{scope}:pull"})
    # Anonymous, pull-only registry token. No AWS credentials or GitHub PAT needed.
    request = Request(
        f"https://ghcr.io/token?{query}", headers={"User-Agent": "cotinga-updater"}
    )
    with urlopen(request, timeout=15) as response:  # nosec B310
        payload = json.loads(response.read(65536))
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise UpdateError("GitHub registry did not return a pull token")
    request = Request(
        f"https://ghcr.io/v2/{scope}/manifests/production",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": ACCEPT,
            "User-Agent": "cotinga-updater",
        },
        method="HEAD",
    )
    with urlopen(request, timeout=15) as response:  # nosec B310
        digest = response.headers.get("Docker-Content-Digest", "")
    if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
        raise UpdateError("GitHub registry returned an invalid image digest")
    return f"{repository}@{digest}"


def read_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise UpdateError("Invalid update state; inspect update-state.json")
    return payload


def write_state(path: Path, state: dict[str, str]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".update-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_update(root: Path, repository: str) -> bool:
    if not (root / ".env").is_file() or not (root / "image.env").is_file():
        raise UpdateError(
            "Deploy the app once and configure its .env before enabling updates"
        )
    with (root / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        image = production_image(repository)
        path = root / "update-state.json"
        state = read_state(path)
        if image in {state.get("applied"), state.get("failed")}:
            return False
        print(f"Deploying tested release {image}", flush=True)
        # The resolver validates the image, and deploy.sh enforces its repository.
        result = subprocess.run(  # nosec B603
            ["/bin/bash", str(root / "deploy.sh"), image],
            env={
                **os.environ,
                "COTINGA_ROOT": str(root),
                "COTINGA_IMAGE_REPOSITORY": repository,
            },
            timeout=840,
            check=False,
        )
        if result.returncode:
            # Busy deployments and failed downloads are retried. Broken releases wait for a new commit.
            if result.returncode != 75:
                state["failed"] = image
                write_state(path, state)
            raise UpdateError(
                f"Release failed (exit {result.returncode}); inspect the deployment logs"
            )
        write_state(path, {"applied": image})
        print("Automatic deployment healthy", flush=True)
        return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("COTINGA_ROOT", "/opt/cotinga")),
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("COTINGA_IMAGE_REPOSITORY", DEFAULT_REPOSITORY),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check public registry access without deploying",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            print(production_image(args.repository))
        else:
            apply_update(args.root, args.repository)
    except HTTPError as exc:
        print(
            f"Registry HTTP {exc.code}: publish the production image and make its GitHub package public",
            file=sys.stderr,
        )
        return 1
    except (
        UpdateError,
        URLError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
