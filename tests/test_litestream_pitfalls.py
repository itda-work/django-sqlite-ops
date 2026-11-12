"""문서에 적은 Litestream 함정을 실제 바이너리로 고정한다(#105).

CLAUDE.md '함정'·README 문제 해결·DESIGN 이 말하는 Litestream 동작 가운데 로컬 서버나 ``file``
복제본으로 재현되는 것을 PATH 의 ``litestream`` 으로 다시 본다. CI ``litestream`` 잡이 검증한 판마다
돌기 때문에, 다음 판에서 동작이 바뀌면 여기서 실패해 문서를 고칠 때임을 알린다.

판마다 다른 동작은 ``*_BY_VERSION`` 표에서 실행 중인 바이너리의 판으로 기대를 고른다. 표에 없는
판이면 실패한다(새 판을 게이트에 넣을 때 다시 잰다). 측정 기록은
``docs/research/lab-2026-11-12-litestream-pitfalls-0.5.18.md`` 다.

연결은 받고 응답하지 않는 서버(SilentServer)에 대한 매달림은 ``test_boot_litestream.py`` 의
``test_real_hanging_s3_endpoint_times_out`` 이 본다.
"""

import http.server
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from _litestream import LITESTREAM, needs_litestream, require_litestream, write_config

from django_sqlite_ops.boot import litestream as ls
from django_sqlite_ops.boot.decide import RemoteEmpty, RemoteError


def running_version() -> str:
    version = ls.version(binary=LITESTREAM)
    assert version is not None, "cannot read litestream version"
    return version


def expectation(table: dict[str, str]) -> str:
    version = running_version()
    if version not in table:
        pytest.fail(f"no expectation for litestream {version}; re-measure (#105) and add it")
    return table[version]


def file_replica(lab: Path) -> str:
    return f"      type: file\n      path: {lab / 'replica'}\n"


def run_ls(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [LITESTREAM, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


# --- 1. endpoint 에 스킴이 없을 때(평문 HTTP 서버) ---------------------------------------------

# 0.5.17: HTTPS 로 접속해 TLS 가 실패해도 재시도하며 끝나지 않는다(60초 넘게 관측).
# 0.5.18: S3 재시도 8회 뒤 rc 1 로 끝난다. ltx 약 7초, restore 약 14초(2026-11-12, macOS arm64).
NO_SCHEME_BY_VERSION = {"0.5.17": "hang", "0.5.18": "exit1"}
NO_SCHEME_LTX_TIMEOUT = 25
NO_SCHEME_RESTORE_TIMEOUT = 40
HTTPS_TO_HTTP = "http: server gave HTTP response to HTTPS client"


class _PlainHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"plain http"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@dataclass
class PlainHttp:
    port: int
    errors: list[str]


@pytest.fixture
def plain_http() -> Iterator[PlainHttp]:
    """평문 HTTP 서버. TLS ClientHello 가 오면 400 을 내고 ``errors`` 에 남긴다."""
    errors: list[str] = []

    class Handler(_PlainHandler):
        def log_error(self, fmt: str, *args: object) -> None:
            errors.append(fmt % args)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield PlainHttp(server.server_address[1], errors)
    finally:
        server.shutdown()
        server.server_close()


def s3_replica(endpoint: str) -> str:
    return (
        "      type: s3\n"
        "      bucket: lab\n"
        "      path: app\n"
        f"      endpoint: {endpoint}\n"
        "      region: us-east-1\n"
        "      access-key-id: x\n"
        "      secret-access-key: y\n"
        "      force-path-style: true\n"
    )


@needs_litestream
def test_no_scheme_endpoint_by_version(tmp_path, plain_http):
    """CLAUDE.md 함정·README 문제 해결: ``endpoint`` 에 ``http://`` 가 빠졌을 때 ltx·restore.

    boot 가 실제로 부르는 경로(``remote_max_txid``·``restore``)로 본다. ltx 와 restore 는 같은
    서버에 동시에 걸어 0.5.17 에서도 테스트 시간이 둘의 합이 되지 않게 한다.
    """
    expected = expectation(NO_SCHEME_BY_VERSION)
    lab = tmp_path.resolve()
    db = lab / "app.db"
    config = write_config(lab, db, s3_replica(f"127.0.0.1:{plain_http.port}"))
    with ThreadPoolExecutor(2) as pool:
        start = time.monotonic()
        ltx = pool.submit(
            ls.remote_max_txid, db, config=config, binary=LITESTREAM, timeout=NO_SCHEME_LTX_TIMEOUT
        )
        restored = pool.submit(
            ls.restore,
            db,
            lab / "out.db",
            config=config,
            binary=LITESTREAM,
            timeout=NO_SCHEME_RESTORE_TIMEOUT,
        )
        remote, restore = ltx.result(), restored.result()
        elapsed = time.monotonic() - start

    # HTTPS 로 접속했다: 평문 서버가 TLS 핸드셰이크 바이트를 HTTP 요청으로 읽고 400 을 냈다.
    assert plain_http.errors, "litestream never reached the plain HTTP server"
    assert type(remote) is RemoteError
    assert restore.ok is False
    if expected == "hang":
        assert f"timed out after {NO_SCHEME_LTX_TIMEOUT}s" in remote.message
        assert "timed out" in restore.reason
    else:
        # boot 의 사유는 300자에서 잘린다. 스킴이 빠졌다는 단서(`Get "https://…`)는 ltx 사유의
        # 앞부분에 있어 남지만, 끝의 HTTPS_TO_HTTP 는 endpoint·prefix 길이에 따라 잘린다.
        assert remote.message.startswith("litestream ltx exited with 1: Error: "), remote.message
        assert "exceeded maximum number of attempts" in remote.message
        assert 'Get "https://127.0.0.1:' in remote.message
        assert restore.reason.startswith("litestream restore exited with 1: Error: ")
        assert "exceeded maximum number of attempts" in restore.reason
        assert elapsed < NO_SCHEME_RESTORE_TIMEOUT


@needs_litestream
def test_no_scheme_endpoint_stderr_names_the_cause(tmp_path, plain_http):
    """Litestream 이 끝나는 판에서는 stderr 끝에 원인(HTTPS 로 평문 서버에 접속)이 그대로 나온다."""
    if expectation(NO_SCHEME_BY_VERSION) == "hang":
        pytest.skip("this version does not exit; covered by test_no_scheme_endpoint_by_version")
    lab = tmp_path.resolve()
    db = lab / "app.db"
    config = write_config(lab, db, s3_replica(f"127.0.0.1:{plain_http.port}"))
    result = run_ls(
        "ltx", "-config", str(config), "-level", "all", "-json", str(db),
        timeout=NO_SCHEME_LTX_TIMEOUT,
    )  # fmt: skip
    assert result.returncode == 1
    assert result.stdout == ""
    (last,) = result.stderr.strip().splitlines()[-1:]
    assert last.startswith("Error: operation error S3: ListObjectsV2")
    assert last.endswith(HTTPS_TO_HTTP)


@needs_litestream
def test_http_scheme_endpoint_reaches_plain_server(tmp_path, plain_http):
    """대조: ``http://`` 를 붙이면 같은 서버에 평문으로 요청하고 바로 끝난다."""
    lab = tmp_path.resolve()
    db = lab / "app.db"
    config = write_config(lab, db, s3_replica(f"http://127.0.0.1:{plain_http.port}"))
    start = time.monotonic()
    result = run_ls("ltx", "-config", str(config), "-level", "all", "-json", str(db))
    assert time.monotonic() - start < 10
    assert (result.returncode, result.stdout.strip()) == (0, "[]")
    assert not plain_http.errors


# --- 3·4·6. restore 와 기존 파일·로컬 메타 ----------------------------------------------------


@dataclass(frozen=True)
class Seeded:
    lab: Path
    db: Path
    config: Path


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Seeded:
    """``file`` 복제본에 행 5개를 복제하고 replicate 를 멈춘 랩(모듈에서 공유, 읽기만 한다)."""
    binary = require_litestream()
    lab = tmp_path_factory.mktemp("seeded").resolve()
    db = lab / "app.db"
    config = write_config(lab, db, file_replica(lab))
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    proc = subprocess.Popen(
        [binary, "replicate", "-config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for i in range(5):
            conn.execute("INSERT INTO t(v) VALUES (?)", (f"row-{i}",))
            conn.commit()
            time.sleep(0.2)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            remote = ls.remote_max_txid(db, config=config, binary=binary)
            local = ls.local_max_txid(db)
            if local is not None and getattr(remote, "txid", None) == local:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"replica did not catch up: remote={remote} local={local}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        conn.close()
    return Seeded(lab, db, config)


def count_rows(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]


@needs_litestream
def test_if_db_not_exists_skips_zero_byte_file(seeded, tmp_path):
    """DESIGN §4-1: 0바이트 DB 가 있으면 ``-if-db-not-exists`` 는 rc 0 으로 복원을 건너뛴다."""
    target = tmp_path / "app.db"
    target.touch()
    result = run_ls(
        "restore", "-config", str(seeded.config), "-if-db-not-exists", "-o", str(target),
        str(seeded.db),
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert 'msg="database already exists, skipping"' in result.stdout
    assert target.stat().st_size == 0

    # 대조: 파일이 없으면 복원한다.
    absent = tmp_path / "absent.db"
    result = run_ls(
        "restore", "-config", str(seeded.config), "-if-db-not-exists", "-o", str(absent),
        str(seeded.db),
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert count_rows(absent) == 5


@needs_litestream
def test_restore_output_zero_byte_is_overwritten_nonempty_refused(seeded, tmp_path):
    """CLAUDE.md 함정: ``restore -o`` 는 0바이트 파일은 덮고 내용 있는 파일은 거부한다."""
    zero = tmp_path / "zero.db"
    zero.touch()
    result = run_ls("restore", "-config", str(seeded.config), "-o", str(zero), str(seeded.db))
    assert result.returncode == 0, result.stderr
    assert count_rows(zero) == 5

    full = tmp_path / "full.db"
    full.write_bytes(b"x" * 100)
    result = run_ls("restore", "-config", str(seeded.config), "-o", str(full), str(seeded.db))
    assert result.returncode == 1
    assert "Error: cannot restore, output path already exists and is not empty" in result.stderr
    assert full.read_bytes() == b"x" * 100


@needs_litestream
def test_restored_db_has_no_local_meta(seeded, tmp_path):
    """CLAUDE.md 함정: restore 로 만든 DB 옆에는 로컬 메타(``.<db>-litestream/``)가 없다."""
    out = tmp_path / "app.db"
    result = run_ls("restore", "-config", str(seeded.config), "-o", str(out), str(seeded.db))
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app.db"]
    assert ls.local_max_txid(out) is None


# --- 7. ltx 는 로컬 메타가 아니라 복제본을 나열한다 --------------------------------------------


@needs_litestream
def test_ltx_lists_replica_not_local_meta(seeded, tmp_path):
    """DESIGN §4-3: 로컬 메타에 LTX 가 있어도 복제본이 비었으면 ``ltx DB_PATH`` 는 ``[]`` 다."""
    lab = tmp_path.resolve()
    db = lab / "app.db"
    db.write_bytes(seeded.db.read_bytes())
    meta = lab / ".app.db-litestream"
    meta.mkdir()
    l0 = meta / "ltx" / "0"
    l0.mkdir(parents=True)
    (src,) = (seeded.lab / ".app.db-litestream" / "ltx" / "0").iterdir()
    (l0 / src.name).write_bytes(src.read_bytes())
    config = write_config(lab, db, file_replica(lab))  # 복제본 디렉터리는 없다
    assert ls.local_max_txid(db) is not None
    assert ls.remote_max_txid(db, config=config, binary=LITESTREAM) == RemoteEmpty()


# --- 5. replicate 가 rollback journal DB 를 WAL 로 바꾼다 --------------------------------------


@needs_litestream
def test_replicate_switches_rollback_journal_db_to_wal(tmp_path):
    """CLAUDE.md 함정: Litestream 을 붙이면 Django 기본(rollback journal) DB 도 WAL 로 돈다."""
    lab = tmp_path.resolve()
    db = lab / "app.db"
    config = write_config(lab, db, file_replica(lab))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t(x)")
    conn.close()

    def mode() -> str:
        conn = sqlite3.connect(db)
        try:
            return conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()

    assert mode() == "delete"
    proc = subprocess.Popen(
        [LITESTREAM, "replicate", "-config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while mode() != "wal":
            assert time.monotonic() < deadline, "journal_mode did not become wal"
            time.sleep(0.2)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # replicate 를 멈춘 뒤에도 파일에 남는다.
    assert mode() == "wal"


def test_expectation_tables_cover_verified_versions():
    assert set(NO_SCHEME_BY_VERSION) == set(ls.VERIFIED_VERSIONS)
