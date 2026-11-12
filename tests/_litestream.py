"""실제 ``litestream`` 바이너리를 쓰는 테스트의 공통 규칙.

바이너리가 PATH 에 없으면 skip 하지만 ``REQUIRE_LITESTREAM=1`` 이면 실패한다(CI 의 litestream 잡).
fixture ``litestream_binary`` 는 ``conftest.py`` 에 있다.
"""

import os
import re
import shutil
import socket
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

LITESTREAM = shutil.which("litestream")
REQUIRE_LITESTREAM = os.environ.get("REQUIRE_LITESTREAM") == "1"


def require_litestream() -> str:
    if LITESTREAM is None:
        if REQUIRE_LITESTREAM:
            pytest.fail("litestream binary not on PATH (REQUIRE_LITESTREAM=1)")
        pytest.skip("litestream binary not on PATH")
    return LITESTREAM


needs_litestream = pytest.mark.usefixtures("litestream_binary")

# --- 판별 실측 fixture --------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
# 검증한 판마다 fixture 폴더(tests/fixtures/litestream-<판>/) 하나와 그 판의 기대값 한 줄.
# boot/litestream.py 의 VERIFIED_VERSIONS 와 같아야 한다(test_boot_litestream 이 본다).
# 값은 그 판의 make_litestream_fixtures.sh 실행 결과다(만든 방법은 각 폴더 README).
FIXTURE_EXPECTED: dict[str, dict[str, int]] = {
    "0.5.17": {"max_txid": 0x19},
    "0.5.18": {"max_txid": 0x19},
}


def fixture_versions() -> list[str]:
    """``tests/fixtures/litestream-*/`` 폴더 이름의 판(숫자 순)."""
    found = [
        d.name.removeprefix("litestream-") for d in FIXTURE_ROOT.glob("litestream-*") if d.is_dir()
    ]
    return sorted(found, key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v)))


@dataclass(frozen=True)
class LitestreamFixtures:
    """한 판의 실측 fixture 폴더와 그 판의 기대값."""

    version: str
    max_txid: int

    @property
    def dir(self) -> Path:
        return FIXTURE_ROOT / f"litestream-{self.version}"

    def out(self, name: str) -> tuple[str, str, int]:
        d = self.dir / name
        return (d / "stdout").read_text(), (d / "stderr").read_text(), int((d / "rc").read_text())

    @property
    def latest_ltx(self) -> Path:
        """replicate 종료 뒤 로컬 메타에 남은 최신 L0(이름 ``<max>-<max>.ltx``)."""
        (path,) = (self.dir / "local_meta_after_replicate" / "meta" / "ltx" / "0").iterdir()
        return path

    @property
    def sample_ltx(self) -> Path:
        """복제본 L1 의 이름순 첫 파일(낮은 TXID 의 실제 LTX)."""
        (path,) = (self.dir / "ltx_sample").iterdir()
        return path

    @property
    def sample_txid(self) -> int:
        return int(self.sample_ltx.name[17:33], 16)


def litestream_fixtures(version: str) -> LitestreamFixtures:
    return LitestreamFixtures(version, **FIXTURE_EXPECTED[version])


def write_config(lab: Path, db: Path, replica_block: str) -> Path:
    config = lab / "litestream.yml"
    config.write_text(
        "l0-retention: 2s\n"
        "l0-retention-check-interval: 1s\n"
        "levels:\n"
        "  - interval: 2s\n"
        "dbs:\n"
        f"  - path: {db}\n"
        "    replica:\n" + replica_block
    )
    return config


class SilentServer:
    """연결은 받지만 아무 응답도 하지 않는 TCP 서버."""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.conns.append(conn)

    def __enter__(self) -> "SilentServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(timeout=5)
        for c in self.conns:
            c.close()
        self.sock.close()
