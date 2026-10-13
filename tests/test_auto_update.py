import fcntl
import json
import subprocess
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from deploy.ec2 import auto_update

IMAGE = "ghcr.io/khoshru/cotinga@sha256:" + "a" * 64


def registry_response(body: object = None, digest: str = "") -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(body).encode()
    response.headers = {"Docker-Content-Digest": digest}
    return response


def test_registry_resolves_production_to_an_exact_digest() -> None:
    with patch(
        "deploy.ec2.auto_update.urlopen",
        side_effect=[
            registry_response({"token": "anonymous-pull-token"}),
            registry_response(digest=IMAGE.split("@", 1)[1]),
        ],
    ) as request:
        assert auto_update.production_image(auto_update.DEFAULT_REPOSITORY) == IMAGE
    token_request = request.call_args_list[0].args[0]
    assert token_request.full_url.startswith("https://ghcr.io/token?")
    assert parse_qs(urlsplit(token_request.full_url).query)["scope"] == [
        "repository:khoshru/cotinga:pull"
    ]
    manifest_request = request.call_args_list[1].args[0]
    assert (
        manifest_request.full_url
        == "https://ghcr.io/v2/khoshru/cotinga/manifests/production"
    )
    assert manifest_request.get_method() == "HEAD"
    assert manifest_request.headers["Authorization"] == "Bearer anonymous-pull-token"
    assert (
        "application/vnd.oci.image.index.v1+json" in manifest_request.headers["Accept"]
    )
    assert all(call.kwargs["timeout"] == 15 for call in request.call_args_list)


@pytest.mark.parametrize(
    "repository",
    [
        "http://example.com/image",
        "ghcr.io/khoshru/cotinga:main",
        "ghcr.io/Khoshru/cotinga",
        "ghcr.io/khoshru/../cotinga",
    ],
)
def test_registry_rejects_other_hosts_and_malformed_repositories(
    repository: str,
) -> None:
    with (
        patch("deploy.ec2.auto_update.urlopen") as request,
        pytest.raises(auto_update.UpdateError, match="repository"),
    ):
        auto_update.production_image(repository)
    request.assert_not_called()


@pytest.mark.parametrize("body", [{}, [], {"token": ""}, {"token": 9}])
def test_registry_rejects_missing_pull_tokens(body: object) -> None:
    with (
        patch("deploy.ec2.auto_update.urlopen", return_value=registry_response(body)),
        pytest.raises(auto_update.UpdateError, match="pull token"),
    ):
        auto_update.production_image(auto_update.DEFAULT_REPOSITORY)


@pytest.mark.parametrize("digest", ["", "sha256:short", "sha256:" + "A" * 64])
def test_registry_rejects_invalid_digests(digest: str) -> None:
    with (
        patch(
            "deploy.ec2.auto_update.urlopen",
            side_effect=[
                registry_response({"token": "pull"}),
                registry_response(digest=digest),
            ],
        ),
        pytest.raises(auto_update.UpdateError, match="digest"),
    ):
        auto_update.production_image(auto_update.DEFAULT_REPOSITORY)


@pytest.fixture
def installed_root(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text("Keep the existing secrets\n")
    (tmp_path / "image.env").write_text("COTINGA_IMAGE=previous\n")
    return tmp_path


def test_successful_update_records_private_state_and_skips_unchanged_release(
    installed_root: Path,
) -> None:
    with (
        patch("deploy.ec2.auto_update.production_image", return_value=IMAGE),
        patch(
            "deploy.ec2.auto_update.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as deploy,
    ):
        assert auto_update.apply_update(installed_root, auto_update.DEFAULT_REPOSITORY)
        assert not auto_update.apply_update(
            installed_root, auto_update.DEFAULT_REPOSITORY
        )
    deploy.assert_called_once()
    assert deploy.call_args.args[0] == [
        "/bin/bash",
        str(installed_root / "deploy.sh"),
        IMAGE,
    ]
    assert deploy.call_args.kwargs["env"]["COTINGA_ROOT"] == str(installed_root)
    assert (
        deploy.call_args.kwargs["env"]["COTINGA_IMAGE_REPOSITORY"]
        == auto_update.DEFAULT_REPOSITORY
    )
    state = installed_root / "update-state.json"
    assert auto_update.read_state(state) == {"applied": IMAGE}
    assert state.stat().st_mode & 0o777 == 0o600
    assert (installed_root / ".env").read_text() == "Keep the existing secrets\n"


def test_failed_release_is_not_repeated_and_keeps_previous_state(
    installed_root: Path,
) -> None:
    state = installed_root / "update-state.json"
    auto_update.write_state(state, {"applied": "previous"})
    with (
        patch("deploy.ec2.auto_update.production_image", return_value=IMAGE),
        patch(
            "deploy.ec2.auto_update.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1),
        ) as deploy,
    ):
        with pytest.raises(auto_update.UpdateError, match="exit 1"):
            auto_update.apply_update(installed_root, auto_update.DEFAULT_REPOSITORY)
        assert not auto_update.apply_update(
            installed_root, auto_update.DEFAULT_REPOSITORY
        )
    deploy.assert_called_once()
    assert auto_update.read_state(state) == {"applied": "previous", "failed": IMAGE}


def test_busy_deployment_is_retried_next_time(installed_root: Path) -> None:
    with (
        patch("deploy.ec2.auto_update.production_image", return_value=IMAGE),
        patch(
            "deploy.ec2.auto_update.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 75),
                subprocess.CompletedProcess([], 0),
            ],
        ) as deploy,
    ):
        with pytest.raises(auto_update.UpdateError, match="exit 75"):
            auto_update.apply_update(installed_root, auto_update.DEFAULT_REPOSITORY)
        assert not (installed_root / "update-state.json").exists()
        assert auto_update.apply_update(installed_root, auto_update.DEFAULT_REPOSITORY)
    assert deploy.call_count == 2


def test_overlapping_checks_do_not_start_another_deployment(
    installed_root: Path,
) -> None:
    with (
        (installed_root / "update.lock").open("a") as lock,
        patch("deploy.ec2.auto_update.production_image") as registry,
    ):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not auto_update.apply_update(
            installed_root, auto_update.DEFAULT_REPOSITORY
        )
    registry.assert_not_called()


def test_update_requires_existing_deployment(tmp_path: Path) -> None:
    with pytest.raises(auto_update.UpdateError, match="Deploy the app once"):
        auto_update.apply_update(tmp_path, auto_update.DEFAULT_REPOSITORY)


@pytest.mark.parametrize("body", [[], {"failed": 4}, "invalid json"])
def test_invalid_state_is_rejected(tmp_path: Path, body: object) -> None:
    state = tmp_path / "update-state.json"
    state.write_text(json.dumps(body) if body != "invalid json" else str(body))
    with pytest.raises((auto_update.UpdateError, ValueError)):
        auto_update.read_state(state)


def test_failed_state_write_leaves_previous_state(tmp_path: Path) -> None:
    path = tmp_path / "update-state.json"
    auto_update.write_state(path, {"applied": "previous"})
    with (
        patch("deploy.ec2.auto_update.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        auto_update.write_state(path, {"applied": IMAGE})
    assert auto_update.read_state(path) == {"applied": "previous"}
    assert not list(tmp_path.glob(".update-state-*"))


def test_check_mode_does_not_deploy(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("deploy.ec2.auto_update.production_image", return_value=IMAGE),
        patch("deploy.ec2.auto_update.apply_update") as deploy,
    ):
        assert auto_update.main(["--check"]) == 0
    assert IMAGE in capsys.readouterr().out
    deploy.assert_not_called()


def test_cli_uses_configured_root_and_repository(
    installed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COTINGA_ROOT", str(installed_root))
    monkeypatch.setenv("COTINGA_IMAGE_REPOSITORY", auto_update.DEFAULT_REPOSITORY)
    with patch("deploy.ec2.auto_update.apply_update") as deploy:
        assert auto_update.main([]) == 0
    deploy.assert_called_once_with(installed_root, auto_update.DEFAULT_REPOSITORY)


@pytest.mark.parametrize(
    "error",
    [
        URLError("offline"),
        ValueError("bad response"),
        HTTPError("https://ghcr.io", 403, "private package", Message(), None),
    ],
)
def test_cli_reports_registry_failures(
    error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("deploy.ec2.auto_update.production_image", side_effect=error):
        assert auto_update.main(["--check"]) == 1
    assert capsys.readouterr().err
