import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "deploy/ec2/deploy.sh"
IMAGE_ID = "sha256:" + "b" * 64


@pytest.fixture
def deployment(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    root = tmp_path / "app"
    root.mkdir()
    (root / "data").mkdir()
    (root / "backups").mkdir()
    (root / ".env").write_text("COTINGA_DOMAIN=example.com\n")
    (root / "compose.yaml").write_text("services: {}\n")
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
    scripts = {
        "git": "echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        "aws": 'echo aws >> "$COTINGA_TEST_LOG"\nexit 99\n',
        "curl": 'echo curl >> "$COTINGA_TEST_LOG"\necho \'{"status":"ok"}\'\n',
        "docker": """printf '%s\n' "$*" >> "$COTINGA_TEST_LOG"
if [[ "$1" == build ]]; then
    exit "${COTINGA_TEST_BUILD_EXIT:-0}"
elif [[ "$1" == image ]]; then
    printf '%s\n' "$COTINGA_TEST_IMAGE_ID"
elif [[ "$*" == *'config --format json' ]]; then
    printf '%s\n' '{"services":{"caddy":{"environment":{"COTINGA_DOMAIN":"example.com"}}}}'
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
    }
    return root, source, log, env


def run_deploy(source: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
