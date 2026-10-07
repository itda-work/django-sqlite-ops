"""``boot/litestream.py`` 테스트 (DESIGN §4-3, §4-4, §11).

- fixture 파싱: ``tests/fixtures/litestream-0.5.17/`` 의 실측 출력(만든 방법은 그 폴더 README).
- 가짜 실행 파일: 테스트가 만든 셸 스크립트로 rc·stderr·타임아웃 처리를 본다.
- 실제 바이너리: ``litestream`` 이 PATH 에 있을 때만 돈다(CI 에는 없어서 skip).
"""

import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
from _nodjango import assert_runs_without_django

from django_sqlite_ops.boot import litestream as ls
from django_sqlite_ops.boot.decide import RemoteEmpty, RemoteError, RemoteTxid

FIXTURES = Path(__file__).parent / "fixtures" / "litestream-0.5.17"
LITESTREAM = shutil.which("litestream")
needs_litestream = pytest.mark.skipif(LITESTREAM is None, reason="litestream binary not on PATH")


def fixture(name: str) -> tuple[str, str, int]:
    d = FIXTURES / name
    return (d / "stdout").read_text(), (d / "stderr").read_text(), int((d / "rc").read_text())


def fake_binary(tmp_path: Path, body: str, *, version: str = "0.5.17") -> str:
    """``version`` 에는 고정 버전을, 그 밖의 명령에는 ``body`` 를 실행하는 가짜 litestream."""
    path = tmp_path / "fake-litestream"
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/argv.log"\n'
        f'if [ "$1" = version ]; then echo "{version}"; exit 0; fi\n' + body + "\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def replay_binary(tmp_path: Path, name: str) -> str:
    """fixture 의 stdout·stderr·rc 를 그대로 내는 가짜 litestream."""
    d = FIXTURES / name
    return fake_binary(tmp_path, f'cat "{d}/stdout"; cat "{d}/stderr" >&2; exit $(cat "{d}/rc")')


def argv_log(tmp_path: Path) -> list[str]:
    return (tmp_path / "argv.log").read_text().splitlines()


# --- fixture 파싱 -----------------------------------------------------------------------


def test_fixture_all_levels_takes_max_over_every_level():
    stdout, _, rc = fixture("ltx_all_levels")
    assert rc == 0
    assert ls.parse_ltx_json(stdout) == RemoteTxid(0x19)


def test_fixture_empty_list_is_remote_empty():
    for name in ("ltx_replica_missing", "ltx_replica_empty_dir"):
        stdout, _, rc = fixture(name)
        assert rc == 0
        assert ls.parse_ltx_json(stdout) == RemoteEmpty(), name


def test_spike_l0_only_misses_compacted_replica():
    """§4-4 스파이크 결함: L0 만 보면(``-level`` 생략) L0 가 사라진 복제본을 빈 목록으로 본다."""
    l0_only, _, _ = fixture("ltx_no_l0_default")
    all_levels, _, _ = fixture("ltx_no_l0_all_levels")
    # 같은 복제본이다. 스파이크처럼 L0 만 보면 '복제본 없음' → 새 DB 로 시작해 원격을 덮는다.
    assert ls.parse_ltx_json(l0_only) == RemoteEmpty()
    assert ls.parse_ltx_json(all_levels) == RemoteTxid(0x19)


def test_remote_query_passes_level_all(tmp_path):
    binary = replay_binary(tmp_path, "ltx_no_l0_all_levels")
    assert ls.remote_max_txid("/data/app.db", config="/etc/ls.yml", binary=binary) == RemoteTxid(
        0x19
    )
    assert argv_log(tmp_path) == [
        "version",
        "ltx -config /etc/ls.yml -level all -json /data/app.db",
    ]


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("ltx_db_not_in_config", "database not found in config"),
        ("ltx_config_missing", "config file not found"),
        ("ltx_bad_yaml", "yaml"),
        ("ltx_permission_denied", "permission denied"),
    ],
)
def test_fixture_nonzero_rc_is_remote_error(tmp_path, name, needle):
    _, _, rc = fixture(name)
    assert rc == 1
    result = ls.remote_max_txid(
        "/data/app.db", config="c.yml", binary=replay_binary(tmp_path, name)
    )
    assert type(result) is RemoteError
    assert result.message.startswith("litestream ltx exited with 1: Error: ")
    assert needle in result.message
    assert "\n" not in result.message


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[",
        "{}",
        '"x"',
        "[1]",
        '[{"level": 0, "min_txid": "0000000000000001"}]',
        '[{"level": "0", "min_txid": "0000000000000001", "max_txid": "0000000000000001"}]',
        '[{"level": 10, "min_txid": "0000000000000001", "max_txid": "0000000000000001"}]',
        '[{"level": true, "min_txid": "0000000000000001", "max_txid": "0000000000000001"}]',
        '[{"level": 0, "min_txid": "0000000000000001", "max_txid": 1}]',
        '[{"level": 0, "min_txid": "0000000000000001", "max_txid": "1"}]',
        '[{"level": 0, "min_txid": "0000000000000001", "max_txid": "000000000000000A"}]',
        '[{"level": 0, "min_txid": "0000000000000002", "max_txid": "0000000000000001"}]',
        '[{"level": 0, "min_txid": "0000000000000001", "max_txid": "0000000000000001"}] trailing',
    ],
)
def test_unexpected_ltx_output_is_remote_error(stdout):
    assert type(ls.parse_ltx_json(stdout)) is RemoteError


def test_ltx_parse_skips_leading_log_lines():
    stdout, _, _ = fixture("ltx_all_levels")
    noisy = 'time=2026-10-07T23:17:15.506+09:00 level=WARN msg="x"\n' + stdout
    assert ls.parse_ltx_json(noisy) == RemoteTxid(0x19)


def test_ltx_parse_ignores_unknown_fields():
    stdout = (
        '[{"level": 3, "min_txid": "0000000000000001", "max_txid": "00000000000000ff", "new": 1}]'
    )
    assert ls.parse_ltx_json(stdout) == RemoteTxid(0xFF)


def test_fixture_version():
    stdout, _, rc = fixture("version")
    assert rc == 0
    assert ls.parse_version(stdout) == "0.5.17"
    assert ls.parse_version("v0.5.17\n") == "0.5.17"
    assert ls.parse_version("litestream 0.5.17") is None


# --- 로컬 메타 --------------------------------------------------------------------------


def test_fixture_local_meta_after_replicate(tmp_path):
    """업로드·L0 정리·종료 뒤에도 메타에 최신 L0 파일이 남는다(실측). 원격 최대와 같다."""
    db = tmp_path / "app.db"
    for rel in (FIXTURES / "local_meta_after_replicate" / "files").read_text().split():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    remote = ls.parse_ltx_json(fixture("ltx_all_levels")[0])
    assert ls.local_max_txid(db) == remote.txid == 0x19


def test_local_meta_takes_max_and_ignores_other_names(tmp_path):
    l0 = tmp_path / ".app.db-litestream" / "ltx" / "0"
    l0.mkdir(parents=True)
    for name in (
        "0000000000000001-0000000000000003.ltx",
        "0000000000000004-000000000000000a.ltx",
        "0000000000000001-00000000000000ff.ltx.tmp",
        "0000000000000001-00000000000000FF.ltx",
        "garbage",
    ):
        (l0 / name).touch()
    # 다른 레벨은 보지 않는다(Litestream DB.MaxLTX 와 같다).
    l1 = l0.parent / "1"
    l1.mkdir()
    (l1 / "0000000000000001-0000000000000fff.ltx").touch()
    assert ls.local_max_txid(tmp_path / "app.db") == 0xA


def test_local_meta_missing_or_empty_is_none(tmp_path):
    db = tmp_path / "app.db"
    assert ls.local_max_txid(db) is None
    (tmp_path / ".app.db-litestream" / "ltx" / "0").mkdir(parents=True)
    assert ls.local_max_txid(db) is None


def test_local_meta_unreadable_is_none(tmp_path):
    l0 = tmp_path / ".app.db-litestream" / "ltx" / "0"
    l0.mkdir(parents=True)
    (l0 / "0000000000000001-0000000000000001.ltx").touch()
    l0.chmod(0)
    try:
        if os.access(l0, os.R_OK):
            pytest.skip("running with permissions that ignore mode bits")
        assert ls.local_max_txid(tmp_path / "app.db") is None
    finally:
        l0.chmod(0o755)


def test_local_meta_custom_path(tmp_path):
    l0 = tmp_path / "meta" / "ltx" / "0"
    l0.mkdir(parents=True)
    (l0 / "0000000000000001-0000000000000007.ltx").touch()
    assert ls.local_max_txid(tmp_path / "app.db", meta_path=tmp_path / "meta") == 7
    assert ls.local_max_txid(tmp_path / "app.db") is None


def test_local_meta_bad_path_is_none():
    assert ls.local_max_txid("bad\0path") is None


# --- 가짜 실행 파일: rc·stderr·타임아웃 ----------------------------------------------------


def test_missing_binary_is_remote_error(tmp_path):
    result = ls.remote_max_txid("app.db", config="c.yml", binary=str(tmp_path / "nope"))
    assert type(result) is RemoteError
    assert "cannot run" in result.message


def test_unsupported_version_is_remote_error(tmp_path):
    binary = fake_binary(tmp_path, "echo '[]'", version="0.5.18")
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary)
    assert result == RemoteError("unsupported litestream version 0.5.18; verified: 0.5.17")
    # 버전이 맞지 않으면 ltx 를 부르지 않는다.
    assert argv_log(tmp_path) == ["version"]


def test_unparseable_version_is_remote_error(tmp_path):
    binary = fake_binary(tmp_path, "echo '[]'", version="development build")
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary)
    assert type(result) is RemoteError
    assert "cannot parse litestream version" in result.message


def test_nonzero_rc_uses_last_stderr_line(tmp_path):
    binary = fake_binary(tmp_path, "echo '[]'; printf 'first\\n\\nError: boom\\n' >&2; exit 3")
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary)
    assert result == RemoteError("litestream ltx exited with 3: Error: boom")


def test_nonzero_rc_without_output(tmp_path):
    binary = fake_binary(tmp_path, "exit 2")
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary)
    assert result == RemoteError("litestream ltx exited with 2: no output")


def test_long_stderr_is_truncated_to_one_line(tmp_path):
    binary = fake_binary(tmp_path, "printf 'Error: %0500d\\n' 0 >&2; exit 1")
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary)
    assert len(result.message) <= 300
    assert result.message.endswith("...")


def test_timeout_is_remote_error(tmp_path):
    binary = fake_binary(tmp_path, "sleep 30")
    start = time.monotonic()
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary, timeout=0.5)
    assert time.monotonic() - start < 5
    assert type(result) is RemoteError
    assert "timed out after 0.5s" in result.message


def test_timeout_kills_grandchildren_holding_pipes(tmp_path):
    # 손자 프로세스가 stdout 을 쥐고 있어도 타임아웃 안에 돌아와야 한다.
    binary = fake_binary(tmp_path, "sleep 30 & sleep 30")
    start = time.monotonic()
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary, timeout=0.5)
    assert time.monotonic() - start < 5
    assert type(result) is RemoteError


def test_version_timeout_is_remote_error(tmp_path):
    path = tmp_path / "hang"
    path.write_text("#!/bin/sh\nsleep 30\n")
    path.chmod(0o755)
    start = time.monotonic()
    result = ls.remote_max_txid("app.db", config="c.yml", binary=str(path), version_timeout=0.5)
    assert time.monotonic() - start < 5
    assert type(result) is RemoteError
    assert "version timed out" in result.message


def test_non_utf8_output_does_not_raise(tmp_path):
    binary = fake_binary(tmp_path, "printf '\\377\\376' ; printf '\\377' >&2; exit 1")
    result = ls.remote_max_txid("app.db", config="c.yml", binary=binary)
    assert type(result) is RemoteError


# --- restore (가짜 실행 파일) --------------------------------------------------------------


def test_restore_passes_expected_args_and_parses_summary(tmp_path):
    out = tmp_path / "restored.db"
    d = FIXTURES / "restore_ok_json"
    # 실측 stdout 은 integrity 로그 줄 뒤에 JSON 이 온다.
    binary = fake_binary(tmp_path, f'cat "{d}/stdout"; printf "SQLite" > "{out}"; exit 0')
    result = ls.restore("/data/app.db", out, config="/etc/ls.yml", binary=binary)
    assert result == ls.RestoreResult(True, f"restored txid 25 to {out}", 0x19)
    assert argv_log(tmp_path)[1] == (
        f"restore -config /etc/ls.yml -json -integrity-check quick -o {out} /data/app.db"
    )
    assert "-if-db-not-exists" not in argv_log(tmp_path)[1]
    assert "-force" not in argv_log(tmp_path)[1]


def test_restore_refuses_existing_output_without_running(tmp_path):
    out = tmp_path / "restored.db"
    out.touch()
    binary = fake_binary(tmp_path, "exit 0")
    result = ls.restore("app.db", out, config="c.yml", binary=binary)
    assert result.ok is False
    assert "already exists" in result.reason
    assert not (tmp_path / "argv.log").exists()


@pytest.mark.parametrize("name", ["restore_no_backups", "restore_output_exists"])
def test_restore_fixture_failures(tmp_path, name):
    result = ls.restore(
        "app.db", tmp_path / "out.db", config="c.yml", binary=replay_binary(tmp_path, name)
    )
    assert result.ok is False
    assert result.reason.startswith("litestream restore exited with 1: Error: ")
    assert result.txid is None


def test_restore_rc0_without_file_is_failure(tmp_path):
    d = FIXTURES / "restore_ok_json"
    binary = fake_binary(tmp_path, f'cat "{d}/stdout"; exit 0')
    result = ls.restore("app.db", tmp_path / "out.db", config="c.yml", binary=binary)
    assert result.ok is False
    assert "missing" in result.reason


def test_restore_rc0_with_empty_file_is_failure(tmp_path):
    out = tmp_path / "out.db"
    d = FIXTURES / "restore_ok_json"
    binary = fake_binary(tmp_path, f'cat "{d}/stdout"; : > "{out}"; exit 0')
    result = ls.restore("app.db", out, config="c.yml", binary=binary)
    assert result.ok is False
    assert "empty" in result.reason


def test_restore_rc0_with_bad_json_is_failure(tmp_path):
    out = tmp_path / "out.db"
    binary = fake_binary(tmp_path, f'echo nope; printf x > "{out}"; exit 0')
    result = ls.restore("app.db", out, config="c.yml", binary=binary)
    assert result.ok is False
    assert "JSON" in result.reason


def test_restore_timeout(tmp_path):
    binary = fake_binary(tmp_path, "sleep 30")
    start = time.monotonic()
    result = ls.restore("app.db", tmp_path / "o.db", config="c.yml", binary=binary, timeout=0.5)
    assert time.monotonic() - start < 5
    assert result.ok is False
    assert "timed out" in result.reason


def test_restore_unsupported_version(tmp_path):
    binary = fake_binary(tmp_path, "exit 0", version="0.6.0")
    result = ls.restore("app.db", tmp_path / "o.db", config="c.yml", binary=binary)
    assert result == ls.RestoreResult(
        False, "unsupported litestream version 0.6.0; verified: 0.5.17"
    )


def test_restore_invalid_integrity_mode(tmp_path):
    result = ls.restore(
        "app.db", tmp_path / "o.db", config="c.yml", binary="litestream", integrity_check="bogus"
    )
    assert result.ok is False


# --- Django 차단 --------------------------------------------------------------------------


def test_module_imports_without_django():
    assert_runs_without_django(
        """
        import django_sqlite_ops.boot.litestream as ls

        assert ls.local_max_txid("/nonexistent/app.db") is None
        """
    )


# --- 실제 바이너리 --------------------------------------------------------------------------


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


@needs_litestream
def test_real_version_is_verified():
    assert ls.check_version(binary=LITESTREAM) is None


@needs_litestream
def test_real_replicate_then_query_and_restore(tmp_path):
    lab = tmp_path.resolve()
    db = lab / "app.db"
    config = write_config(lab, db, f"      type: file\n      path: {lab / 'replica'}\n")

    # 복제 전: 복제본 없음 → 빈 목록, 로컬 메타 없음.
    assert ls.remote_max_txid(db, config=config, binary=LITESTREAM) == RemoteEmpty()
    assert ls.local_max_txid(db) is None

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    proc = subprocess.Popen(
        [LITESTREAM, "replicate", "-config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for i in range(10):
            conn.execute("INSERT INTO t(v) VALUES (?)", (f"row-{i}" * 100,))
            conn.commit()
            time.sleep(0.3)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            remote = ls.remote_max_txid(db, config=config, binary=LITESTREAM)
            local = ls.local_max_txid(db)
            if type(remote) is RemoteTxid and local is not None and remote.txid == local:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"replica did not catch up: remote={remote} local={local}")
        # L0 보존 정리(2s)·압축이 돈 뒤에도 같아야 한다.
        time.sleep(5)
        remote = ls.remote_max_txid(db, config=config, binary=LITESTREAM)
        assert type(remote) is RemoteTxid
        assert ls.local_max_txid(db) == remote.txid
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        conn.close()

    # 종료 뒤에도 로컬 메타에 최신 TXID 가 남는다.
    assert ls.local_max_txid(db) == remote.txid

    out = lab / "restore" / "restored.db"
    result = ls.restore(db, out, config=config, binary=LITESTREAM)
    assert result.ok, result.reason
    assert result.txid == remote.txid
    with sqlite3.connect(out) as restored:
        assert restored.execute("SELECT count(*) FROM t").fetchone() == (10,)

    # 같은 경로로 다시 복원하지 않는다.
    again = ls.restore(db, out, config=config, binary=LITESTREAM)
    assert again.ok is False


@needs_litestream
def test_real_db_not_in_config_is_remote_error(tmp_path):
    lab = tmp_path.resolve()
    config = write_config(lab, lab / "app.db", f"      type: file\n      path: {lab / 'r'}\n")
    result = ls.remote_max_txid(lab / "other.db", config=config, binary=LITESTREAM)
    assert type(result) is RemoteError
    assert "database not found in config" in result.message


@needs_litestream
def test_real_restore_without_backups_fails(tmp_path):
    lab = tmp_path.resolve()
    config = write_config(lab, lab / "app.db", f"      type: file\n      path: {lab / 'r'}\n")
    result = ls.restore(lab / "app.db", lab / "out.db", config=config, binary=LITESTREAM)
    assert result.ok is False
    assert "no matching backup files" in result.reason


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


@needs_litestream
@pytest.mark.parametrize("scheme", ["", "http://"])
def test_real_hanging_s3_endpoint_times_out(tmp_path, scheme):
    """CLAUDE.md 함정: 응답 없는 endpoint 에서 litestream 은 무한 대기한다. 시간 안에 끊는다."""
    lab = tmp_path.resolve()
    with SilentServer() as server:
        config = write_config(
            lab,
            lab / "app.db",
            "      type: s3\n"
            "      bucket: lab\n"
            "      path: app\n"
            f"      endpoint: {scheme}127.0.0.1:{server.port}\n"
            "      region: us-east-1\n"
            "      access-key-id: x\n"
            "      secret-access-key: y\n"
            "      force-path-style: true\n",
        )
        start = time.monotonic()
        result = ls.remote_max_txid(lab / "app.db", config=config, binary=LITESTREAM, timeout=3)
        elapsed = time.monotonic() - start
        assert server.conns, "litestream never connected"
    assert type(result) is RemoteError
    assert "timed out after 3s" in result.message
    assert elapsed < 3 + 6
