import fcntl
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "deploy/ec2/deploy.sh"
IMAGE_ID = "sha256:" + "b" * 64
GHCR_IMAGE = "ghcr.io/khoshru/cotinga@sha256:" + "c" * 64
REVISION = "a" * 40


@pytest.fixture
def deployment(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    root = tmp_path / "app"
    root.mkdir()
    (root / "data").mkdir()
    (root / "backups").mkdir()
    (root / ".env").write_text("COTINGA_DOMAIN=example.com\n")
    (root / "compose.yaml").write_text("services: {}\n")
    (root / "Caddyfile").write_text("Previous Caddy configuration\n")
    (root / "deploy.sh").write_text("#!/bin/bash\necho previous\n")
    (root / "backup_sqlite.py").write_bytes(
        (PROJECT_ROOT / "deploy/ec2/backup_sqlite.py").read_bytes()
    )
    (root / "image.env").write_text("COTINGA_IMAGE=cotinga:previous\n")
    with closing(sqlite3.connect(root / "data/db.sqlite3")) as connection:
        connection.execute("CREATE TABLE meals (description TEXT)")
        connection.execute("INSERT INTO meals VALUES ('Keep my meal')")
        connection.commit()
    source = tmp_path / "source checkout"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    (source / "uv.lock").write_text("")
    binary = tmp_path / "bin"
    binary.mkdir()
    assets = tmp_path / "release"
    assets.mkdir()
    for name in [
        "compose.yaml",
        "Caddyfile",
        "deploy.sh",
        "auto_update.py",
        "backup_sqlite.py",
    ]:
        (assets / name).write_bytes((PROJECT_ROOT / "deploy/ec2" / name).read_bytes())
    scripts = {
        "git": "echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        "aws": 'echo aws >> "$COTINGA_TEST_LOG"\nexit 99\n',
        "curl": """echo curl >> "$COTINGA_TEST_LOG"
if [[ -n "${COTINGA_TEST_LATE_MEAL:-}" ]]; then
    python3 -c 'import os, sqlite3; c=sqlite3.connect(os.environ["COTINGA_ROOT"]+"/data/db.sqlite3"); c.execute("INSERT INTO meals VALUES (?)", ("Written during failed release",)); c.commit(); c.close()'
fi
if [[ "${COTINGA_TEST_CURL_EXIT:-0}" != 0 ]]; then exit "$COTINGA_TEST_CURL_EXIT"; fi
printf '{"status":"ok","revision":"%s"}\n' "${COTINGA_TEST_HEALTH_REVISION:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
""",
        "docker": """printf '%s\n' "$*" >> "$COTINGA_TEST_LOG"
if [[ "$1" == build ]]; then
    exit "${COTINGA_TEST_BUILD_EXIT:-0}"
elif [[ "$1" == image ]]; then
    if [[ "$4" == '{{.Id}}' ]]; then
        printf '%s\n' "$COTINGA_TEST_IMAGE_ID"
    else
        printf '%s\n' "$COTINGA_TEST_LABELS"
    fi
elif [[ "$1" == pull ]]; then
    exit "${COTINGA_TEST_PULL_EXIT:-0}"
elif [[ "$1" == create ]]; then
    echo test-container
elif [[ "$1" == cp ]]; then
    cp -P "$COTINGA_TEST_ASSETS/${2##*/}" "$3"
elif [[ "$*" == *'config --format json' ]]; then
    printf '{"services":{"caddy":{"environment":{"COTINGA_DOMAIN":"%s"}}}}\n' "${COTINGA_TEST_DOMAIN:-example.com}"
elif [[ "$*" == *'up --detach'* && -n "${COTINGA_TEST_START_EXIT:-}" && ! -f "$COTINGA_ROOT/start-failed" ]]; then
    touch "$COTINGA_ROOT/start-failed"
    exit "$COTINGA_TEST_START_EXIT"
fi
""",
    }
    for name, body in scripts.items():
        executable = binary / name
        executable.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        executable.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "COTINGA_ROOT": str(root),
        "COTINGA_TEST_LOG": str(log),
        "COTINGA_TEST_IMAGE_ID": IMAGE_ID,
        "COTINGA_TEST_ASSETS": str(assets),
        "COTINGA_TEST_LABELS": json.dumps(
            {
                "org.opencontainers.image.source": "https://github.com/khoshru/cotinga",
                "org.opencontainers.image.revision": REVISION,
            }
        ),
    }
    return root, source, log, env


def run_deploy(
    source: Path | str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_source_deployment_needs_no_aws_credentials_and_backs_up_data(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, source, log, env = deployment
    result = run_deploy(source, env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert "aws" not in calls
    assert not any(call.startswith("pull ") for call in calls)
    build = next(index for index, call in enumerate(calls) if call.startswith("build "))
    stop = next(index for index, call in enumerate(calls) if "stop --timeout" in call)
    start = next(index for index, call in enumerate(calls) if "up --detach" in call)
    assert build < stop < start
    assert (root / "image.env").read_text() == f"COTINGA_IMAGE={IMAGE_ID}\n"
    assert (root / "image.env").stat().st_mode & 0o777 == 0o600
    assert (
        list((root / "backups").glob("image-*.env"))[0].read_text()
        == "COTINGA_IMAGE=cotinga:previous\n"
    )
    backup = list((root / "backups").glob("db-*.sqlite3"))[0]
    for database in [backup, root / "data/db.sqlite3"]:
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT description FROM meals").fetchone() == (
                "Keep my meal",
            )


def test_failed_build_keeps_current_app_and_release(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, source, log, env = deployment
    result = run_deploy(source, {**env, "COTINGA_TEST_BUILD_EXIT": "7"})
    assert result.returncode == 7
    assert (root / "image.env").read_text() == "COTINGA_IMAGE=cotinga:previous\n"
    assert list((root / "backups").iterdir()) == []
    assert "stop --timeout" not in log.read_text()
    assert "up --detach" not in log.read_text()


def test_github_release_updates_host_files_without_touching_secrets(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, _, log, env = deployment
    result = run_deploy(GHCR_IMAGE, env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert "aws" not in calls
    assert calls[0] == f"pull {GHCR_IMAGE}"
    assert calls.index("rm test-container") < next(
        i for i, call in enumerate(calls) if "stop --timeout" in call
    )
    assert (root / "image.env").read_text() == f"COTINGA_IMAGE={GHCR_IMAGE}\n"
    assert (root / ".env").read_text() == "COTINGA_DOMAIN=example.com\n"
    for name in [
        "compose.yaml",
        "Caddyfile",
        "deploy.sh",
        "auto_update.py",
        "backup_sqlite.py",
    ]:
        assert (root / name).read_bytes() == (
            PROJECT_ROOT / "deploy/ec2" / name
        ).read_bytes()
    assert not list(root.glob("candidate.*"))
    assert any("--force-recreate" in call and call.endswith(" caddy") for call in calls)
    assert all(
        f"--project-directory {root}" in call
        for call in calls
        if call.startswith("compose ")
    )


@pytest.mark.parametrize(
    "labels",
    [
        None,
        {},
        {
            "org.opencontainers.image.source": "https://github.com/another/repo",
            "org.opencontainers.image.revision": REVISION,
        },
    ],
)
def test_untrusted_image_labels_fail_before_stopping_current_app(
    deployment: tuple[Path, Path, Path, dict[str, str]],
    labels: object,
) -> None:
    root, _, log, env = deployment
    result = run_deploy(GHCR_IMAGE, {**env, "COTINGA_TEST_LABELS": json.dumps(labels)})
    assert result.returncode != 0
    assert "stop --timeout" not in log.read_text()
    assert (root / "image.env").read_text() == "COTINGA_IMAGE=cotinga:previous\n"


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/another/repo@sha256:" + "c" * 64,
        "ghcr.io/khoshru/cotinga:production",
        "ghcr.io/khoshru/cotinga@sha256:bad",
    ],
)
def test_only_configured_repository_and_exact_digest_are_accepted(
    deployment: tuple[Path, Path, Path, dict[str, str]],
    image: str,
) -> None:
    root, _, log, env = deployment
    result = run_deploy(image, env)
    assert result.returncode == 1
    assert not log.exists()
    assert (root / "image.env").read_text() == "COTINGA_IMAGE=cotinga:previous\n"


def test_failed_image_pull_leaves_running_app_alone(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, _, log, env = deployment
    result = run_deploy(GHCR_IMAGE, {**env, "COTINGA_TEST_PULL_EXIT": "9"})
    assert result.returncode == 75
    assert "retry without stopping" in result.stderr
    assert "stop --timeout" not in log.read_text()
    assert list((root / "backups").iterdir()) == []


def test_release_symlinks_are_rejected_before_stopping_app(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, _, log, env = deployment
    asset = Path(env["COTINGA_TEST_ASSETS"]) / "auto_update.py"
    asset.unlink()
    asset.symlink_to(PROJECT_ROOT / "deploy/ec2/auto_update.py")
    result = run_deploy(GHCR_IMAGE, env)
    assert result.returncode == 1
    assert "regular deployment file" in result.stderr
    assert "stop --timeout" not in log.read_text()
    assert not list(root.glob("candidate.*"))


@pytest.mark.parametrize(
    "failure",
    [
        {"COTINGA_TEST_START_EXIT": "8"},
        {"COTINGA_TEST_CURL_EXIT": "22"},
        {"COTINGA_TEST_HEALTH_REVISION": "b" * 40},
        {"COTINGA_TEST_DOMAIN": "bad/domain"},
    ],
)
def test_failed_release_restores_host_files_and_previous_image(
    deployment: tuple[Path, Path, Path, dict[str, str]],
    failure: dict[str, str],
) -> None:
    root, _, log, env = deployment
    previous = {
        name: (root / name).read_bytes()
        for name in [
            "compose.yaml",
            "Caddyfile",
            "backup_sqlite.py",
            "deploy.sh",
            "image.env",
        ]
    }
    result = run_deploy(GHCR_IMAGE, {**env, **failure})
    assert result.returncode != 0
    assert result.stderr.count("Deployment failed; attempting") == 1
    for name, content in previous.items():
        assert (root / name).read_bytes() == content
    calls = log.read_text().splitlines()
    assert sum("stop --timeout" in call for call in calls) == 2
    assert list((root / "backups").glob("db-*.sqlite3"))
    assert not list(root.glob("candidate.*"))


def test_recovery_does_not_erase_writes_made_during_failed_release(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, _, _, env = deployment
    result = run_deploy(
        GHCR_IMAGE,
        {**env, "COTINGA_TEST_CURL_EXIT": "22", "COTINGA_TEST_LATE_MEAL": "1"},
    )
    assert result.returncode == 22
    with closing(sqlite3.connect(root / "data/db.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM meals").fetchone() == (2,)
    backup = list((root / "backups").glob("db-*.sqlite3"))[0]
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM meals").fetchone() == (1,)


def test_deployment_lock_prevents_overlapping_replacements(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, _, log, env = deployment
    with (root / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_deploy(GHCR_IMAGE, env)
    assert result.returncode == 75
    assert not log.exists()
    assert list((root / "backups").iterdir()) == []


def test_invalid_source_fails_before_docker(
    deployment: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    root, source, log, env = deployment
    (source / "uv.lock").unlink()
    result = run_deploy(source, env)
    assert result.returncode == 1
    assert "source checkout" in result.stderr
    assert not log.exists()
    assert (root / "image.env").read_text() == "COTINGA_IMAGE=cotinga:previous\n"
