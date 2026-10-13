"""Exercise the release's Caddy binary and configuration before publication."""

import json
import subprocess
import sys
import tempfile
import time
from http.client import HTTPConnection
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def request(
    connection: HTTPConnection,
    path: str,
    method: str = "GET",
    forwarded_ip: str = "203.0.113.1",
) -> tuple[int, str | None]:
    connection.request(
        method,
        path,
        headers={"X-Forwarded-For": forwarded_ip, "X-Real-IP": forwarded_ip},
    )
    response = connection.getresponse()
    response.read()
    return response.status, response.getheader("Retry-After")


def check_limits(image: str) -> None:
    container = ""
    connections: list[HTTPConnection] = []
    with tempfile.TemporaryDirectory(prefix="cotinga-caddy-check-") as temporary:
        config = Path(temporary) / "Caddyfile"
        # Keep the real policies; replace TLS listeners and Django with a tiny response.
        config.write_text(
            (PROJECT_ROOT / "deploy/ec2/Caddyfile")
            .read_text()
            .replace("http://{$COTINGA_DOMAIN} {", ":8081 {")
            .replace("{$COTINGA_DOMAIN} {", ":8080 {")
            .replace("reverse_proxy web:8000", 'respond "ok" 200')
        )
        try:
            container = docker(
                "run",
                "--detach",
                "--read-only",
                "--user",
                "0:0",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/data",
                "--tmpfs",
                "/config",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_BIND_SERVICE",
                "--security-opt",
                "no-new-privileges:true",
                "--env",
                "COTINGA_DOMAIN=example.com",
                "--env",
                "COTINGA_TLS_EMAIL=ci@example.com",
                "--env",
                "XDG_CONFIG_HOME=/config",
                "--env",
                "XDG_DATA_HOME=/data",
                "--publish",
                "127.0.0.1::8080",
                "--publish",
                "127.0.0.1::8081",
                "--volume",
                f"{config}:/etc/caddy/Caddyfile:ro",
                "--entrypoint",
                "caddy",
                image,
                "run",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            )
            ports = json.loads(
                docker(
                    "inspect", "--format", "{{json .NetworkSettings.Ports}}", container
                )
            )
            connections = [
                HTTPConnection("127.0.0.1", int(ports[port][0]["HostPort"]), timeout=3)
                for port in ["8080/tcp", "8081/tcp"]
            ]
            main, redirect = connections
            for attempt in range(50):
                try:
                    assert request(main, "/")[0] == 200
                    break
                except OSError:
                    if attempt == 49:
                        raise
                    time.sleep(0.1)
            time.sleep(1.1)

            # HTTP and HTTPS routes share 30 events, even with forged proxy headers.
            started = time.monotonic()
            for index in range(30):
                connection = main if index % 2 == 0 else redirect
                expected = 200 if connection is main else 308
                status, _ = request(
                    connection, f"/path-{index}", forwarded_ip=f"203.0.113.{index + 1}"
                )
                assert status == expected, (index, status)
            status, retry = request(main, "/statistics/")
            assert time.monotonic() - started < 1, "Requests were too slow for a burst"
            assert status == 429 and retry is not None
            assert request(redirect, "/")[0] == 429
            time.sleep(1.1)
            assert request(main, "/")[0] == 200

            # The five-event bucket combines both login forms, methods, and URL variants.
            started = time.monotonic()
            for index, path in enumerate(
                [
                    "/accounts/login/",
                    "/admin/login/",
                    "/accounts/login?next=/",
                    "/admin/login/?next=/admin/",
                    "/accounts/%6cogin/",
                ]
            ):
                assert request(main, path, "POST" if index % 2 else "GET")[0] == 200
            status, retry = request(main, "/admin/login/", "POST")
            assert time.monotonic() - started < 1, "Requests were too slow for a burst"
            assert status == 429 and retry is not None
            assert request(main, "/accounts/login/")[0] == 429
            assert request(redirect, "/admin/login/")[0] == 429
            assert request(main, "/weight/")[0] == 200
            time.sleep(1.1)
            assert request(main, "/accounts/login/", "POST")[0] == 200
            print("Caddy limits passed: 30/s general; 5/s shared by both login routes.")
        except Exception:
            if container:
                print(docker("logs", container), file=sys.stderr)
            raise
        finally:
            for connection in connections:
                connection.close()
            if container:
                docker("rm", "--force", container)


if __name__ == "__main__":
    check_limits(sys.argv[1])
