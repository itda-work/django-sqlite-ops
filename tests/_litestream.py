"""실제 ``litestream`` 바이너리를 쓰는 테스트의 공통 규칙.

바이너리가 PATH 에 없으면 skip 하지만 ``REQUIRE_LITESTREAM=1`` 이면 실패한다(CI 의 litestream 잡).
fixture ``litestream_binary`` 는 ``conftest.py`` 에 있다.
"""

import os
import shutil
import socket
import threading
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
