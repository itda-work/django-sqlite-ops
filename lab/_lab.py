"""회귀 랩 하네스의 공통 도구: compose 호출, toxiproxy, 볼륨·복제본 조사, HTTP.

Docker 는 ``docker compose -p <우리 프로젝트>`` 로만 다룬다. 컨테이너 하나를 직접 죽일 때도
``docker compose kill`` 을 쓴다. 이 모듈은 ``python3 lab/_lab.py up main`` 으로 기반 스택만
띄울 수도 있다(``scripts/lab.sh up``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "lab" / ".build"
PROJECT = os.environ.get("LAB_PROJECT", "dso-lab")
TAG = os.environ.get("LAB_TAG", "dev")
BUCKET = "dso-lab"

if not re.fullmatch(r"dso-lab(-[a-z0-9][a-z0-9-]*)?", PROJECT):
    raise SystemExit(f"LAB_PROJECT must be dso-lab or dso-lab-<suffix>: {PROJECT}")

STACK_FILES = {
    "main": ["lab-compose.yaml"],
    "p1": ["lab-compose.yaml", "compose.yaml", "profile-override.yaml"],
    "p2": [
        "lab-compose.yaml",
        "compose-multiproc.yaml",
        "profile-override.yaml",
        "multiproc-override.yaml",
    ],
}

# toxiproxy 프록시: s3 는 boot·replicate 가, s3h 는 L8b 의 헬스 조회만 쓴다.
PROXIES = {"s3": 18333, "s3h": 18334}


def log(message: str) -> None:
    print(f"[lab] {time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


@dataclass
class Stack:
    name: str = "main"
    env: dict[str, str] = field(default_factory=dict)

    @property
    def project(self) -> str:
        return PROJECT if self.name == "main" else f"{PROJECT}-{self.name}"

    def argv(self) -> list[str]:
        argv = ["docker", "compose", "-p", self.project, "--project-directory", str(BUILD)]
        for f in STACK_FILES[self.name]:
            argv += ["-f", str(BUILD / f)]
        return argv

    def compose(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        timeout: float = 300,
    ) -> Result:
        full_env = {**os.environ, "LAB_TAG": TAG, **self.env, **(env or {})}
        proc = subprocess.run(
            [*self.argv(), *args],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        res = Result(proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            raise RuntimeError(f"compose {' '.join(args)} -> rc {proc.returncode}\n{res.text}")
        return res

    # --- 기반 서비스 ---------------------------------------------------------------------

    def up_infra(self) -> None:
        self.compose("up", "-d", "--wait", "s3", "toxiproxy", timeout=600)
        self.create_bucket()
        self.setup_proxies()

    def create_bucket(self) -> None:
        for _ in range(30):
            res = self.compose(
                "exec",
                "-T",
                "s3",
                "sh",
                "-c",
                f"echo 's3.bucket.create -name {BUCKET}' | weed shell",
                check=False,
            )
            listing = self.compose(
                "exec", "-T", "s3", "sh", "-c", "echo 's3.bucket.list' | weed shell", check=False
            )
            if BUCKET in listing.out:
                return
            time.sleep(1)
        raise RuntimeError(f"cannot create bucket: {res.text}")

    def toxiproxy_url(self) -> str:
        port = self.compose("port", "toxiproxy", "8474").out.strip().rsplit(":", 1)[1]
        return f"http://127.0.0.1:{port}"

    def toxi(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.toxiproxy_url() + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def setup_proxies(self) -> None:
        existing = self.toxi("GET", "/proxies") or {}
        for name, port in PROXIES.items():
            if name in existing:
                self.toxi("DELETE", f"/proxies/{name}")
            self.toxi(
                "POST",
                "/proxies",
                {"name": name, "listen": f"0.0.0.0:{port}", "upstream": "s3:8333"},
            )

    def proxy_enabled(self, name: str, enabled: bool) -> None:
        self.toxi("POST", f"/proxies/{name}", {"enabled": enabled})
        log(f"toxiproxy {name} enabled={enabled}")

    def add_toxic(self, proxy: str, toxic: dict) -> None:
        self.toxi("POST", f"/proxies/{proxy}/toxics", toxic)
        log(f"toxiproxy {proxy} + {toxic['type']}")

    def remove_toxic(self, proxy: str, name: str) -> None:
        self.toxi("DELETE", f"/proxies/{proxy}/toxics/{name}")

    def reset_proxies(self) -> None:
        self.toxi("POST", "/reset")

    def down(self) -> None:
        self.compose(
            "--profile",
            "app",
            "down",
            "-v",
            "--remove-orphans",
            "--timeout",
            "30",
            check=False,
            timeout=600,
        )

    # --- 앱 컨테이너 ---------------------------------------------------------------------

    def container_id(self, service: str) -> str | None:
        ids = self.compose("ps", "-a", "-q", service).out.split()
        return ids[0] if ids else None

    def state(self, service: str) -> dict:
        cid = self.container_id(service)
        if cid is None:
            return {}
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{json .State}}", cid],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return json.loads(out)

    def wait_exit(self, service: str, timeout: float = 120) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.state(service)
            if st and st.get("Status") == "exited":
                return int(st["ExitCode"])
            time.sleep(0.3)
        raise TimeoutError(f"{service} did not exit in {timeout}s")

    def logs(self, service: str) -> str:
        return self.compose("logs", "--no-color", "--no-log-prefix", service, check=False).text

    def host_port(self, service: str) -> int:
        out = self.compose("port", service, "8000").out.strip()
        return int(out.rsplit(":", 1)[1])

    def wait_http(self, service: str, path: str = "/lab/count", timeout: float = 120) -> int:
        """앱이 응답할 때까지 기다리고 호스트 포트를 돌려준다. 그 전에 죽으면 실패."""
        deadline = time.monotonic() + timeout
        port = None
        while time.monotonic() < deadline:
            st = self.state(service)
            if st.get("Status") == "exited":
                raise RuntimeError(
                    f"{service} exited with {st.get('ExitCode')}\n{self.logs(service)}"
                )
            try:
                port = port or self.host_port(service)
                http_json("GET", port, path, timeout=2)
                return port
            except (OSError, RuntimeError, ValueError, urllib.error.URLError):
                time.sleep(0.5)
        raise TimeoutError(f"{service} not serving in {timeout}s\n{self.logs(service)}")

    def up_app(self, service: str = "labapp", *, env: dict[str, str]) -> int:
        """앱을 새로 띄우고 응답할 때까지 기다린다. 호스트 포트를 돌려준다."""
        self.compose("up", "-d", "--no-deps", "--force-recreate", service, env=env)
        return self.wait_http(service)

    def start_app(self, service: str = "labapp", *, env: dict[str, str]) -> None:
        """띄우기만 한다(거부·kill 시나리오)."""
        self.compose("up", "-d", "--no-deps", "--force-recreate", service, env=env)

    def run_boot(self, service: str = "labapp", *, env: dict[str, str], timeout=300) -> Result:
        """거부·실패가 예상되는 부팅. 끝날 때까지 기다리고 종료 코드와 로그를 돌려준다."""
        res = self.compose(
            "run", "--rm", "--no-deps", "-T", service, env=env, check=False, timeout=timeout
        )
        return res

    def stop_app(self, service: str = "labapp") -> tuple[int, float, str]:
        """``docker stop``(SIGTERM). (종료 코드, 걸린 초, 로그)."""
        t0 = time.monotonic()
        self.compose("stop", "-t", "30", service, timeout=120)
        took = time.monotonic() - t0
        return int(self.state(service)["ExitCode"]), took, self.logs(service)

    def kill_app(self, service: str = "labapp") -> None:
        """SIGKILL. 이 프로젝트의 서비스 컨테이너만 대상이다."""
        self.compose("kill", "-s", "SIGKILL", service)
        self.wait_exit(service, timeout=30)

    def volume_python(self, volume: str, code: str, *extra: str) -> Result:
        return self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            *extra,
            "--entrypoint",
            "python",
            "labapp",
            "-c",
            code,
            env={"LAB_VOLUME": volume},
        )

    def copy_volume(self, src: str, dst: str) -> None:
        """우리 볼륨 src 의 내용을 빈 볼륨 dst 로 복사한다(같은 소유자·권한)."""
        self.volume_python(
            dst,
            "import shutil; shutil.copytree('/src', '/data', symlinks=True, dirs_exist_ok=True)",
            "-v",
            f"{self.project}_{src}:/src:ro",
        )

    def remove_meta(self, volume: str) -> None:
        """로컬 Litestream 메타 디렉터리 하나만 지운다(L3)."""
        self.volume_python(volume, "import shutil; shutil.rmtree('/data/.app.sqlite3-litestream')")

    def volume_exists(self, volume: str) -> bool:
        proc = subprocess.run(
            ["docker", "volume", "inspect", f"{self.project}_{volume}"],
            capture_output=True,
        )
        return proc.returncode == 0

    def inspect_volume(self, volume: str) -> dict:
        res = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "labapp",
            "/app/lab_tools/inspect_data.py",
            env={"LAB_VOLUME": volume},
        )
        return json.loads(res.out.strip().splitlines()[-1])

    def replica(self, prefix: str) -> dict:
        res = self.compose(
            "run", "--rm", "--no-deps", "-T", "tool", env={"LAB_PREFIX": prefix}, timeout=300
        )
        return json.loads(res.out.strip().splitlines()[-1])

    def s3_objects(self, prefix: str, *args: str) -> Result:
        return self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "tool",
            "/app/lab_tools/s3_objects.py",
            prefix,
            *args,
        )


def http_json(
    method: str, port: int, path: str, *, timeout: float = 10, body: bytes | None = None
) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def boot_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[boot]")]


def decision(text: str) -> str | None:
    for line in boot_lines(text):
        if line.startswith("[boot] decision:"):
            return line
    return None


if __name__ == "__main__":
    if sys.argv[1:] == ["up", "main"]:
        Stack("main").up_infra()
        print(f"up: {PROJECT} (toxiproxy {Stack('main').toxiproxy_url()})")
    else:
        raise SystemExit("usage: python3 lab/_lab.py up main")
