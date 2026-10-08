"""복제 헬스 (DESIGN §7).

상태 계산은 순수 함수 표 테스트로, 스레드는 가짜 조회 함수로, 뷰는 새 인터프리터의 Django
(``settings.configure()``, ``test_checks.py``·``test_doctor.py`` 와 같은 방식)로 본다.
실제 Litestream 은 ``file://`` 복제본으로 본다(S3 끊김 L8 은 랩 #10 몫).
"""

import json
import os
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _litestream import needs_litestream

from django_sqlite_ops import health
from django_sqlite_ops.boot import litestream
from django_sqlite_ops.boot.decide import RemoteEmpty, RemoteError, RemoteTxid
from django_sqlite_ops.boot.litestream import LtxWalRange, local_max_ltx, ltx_wal_range
from django_sqlite_ops.health import (
    BACKLOG,
    CAUGHT_UP,
    UNKNOWN,
    AliasConfig,
    FileTimes,
    HealthConfig,
    Monitor,
    Sample,
    Since,
    alias_status,
    files_went_backwards,
    next_backlog_since,
    next_pending_since,
    next_wal_pending_since,
    overall_status,
    parse_config,
    read_boot_state,
    redact,
)
from django_sqlite_ops.wal import IN_SYNC, NO_EVIDENCE, PENDING, WalEvidence, read_wal_header
from django_sqlite_ops.wal import _checksum as wal_checksum
from django_sqlite_ops.wal import compare as wal_compare

ROOT = Path(__file__).resolve().parent.parent
SQLITE = {"ENGINE": "django.db.backends.sqlite3", "NAME": "/srv/app/app.sqlite3"}

# --- next_backlog_since · next_pending_since -----------------------------------------


def since(mono, wall=None, key=None):
    return Since(mono, mono if wall is None else wall, key)


@pytest.mark.parametrize(
    ("prev", "local", "remote", "expected"),
    [
        (None, 5, RemoteTxid(5), None),  # 같음
        (since(10.0, key=6), 5, RemoteTxid(5), None),  # 따라잡으면 지운다
        (None, 6, RemoteTxid(5), since(100.0, 7.0, 6)),  # 앞서기 시작: 그때의 로컬 TXID
        (since(40.0, key=6), 6, RemoteTxid(5), since(40.0, key=6)),  # 이어지면 유지
        (since(40.0, key=6), 9, RemoteTxid(5), since(40.0, key=6)),  # 6 이 아직 안 올라감
        # 6 이 올라갔다(원격 6) → 지금의 로컬 9 로 다시 센다: 바쁜 DB 에서 쌓이지 않는다
        (since(40.0, key=6), 9, RemoteTxid(6), since(100.0, 7.0, 9)),
        (since(40.0, key=6), 9, RemoteTxid(8), since(100.0, 7.0, 9)),
        (since(40.0, key=6), 4, RemoteTxid(5), None),  # 원격 앞섬
        (since(40.0, key=6), None, RemoteTxid(5), None),  # 메타 없음
        (since(40.0, key=6), 6, RemoteEmpty(), None),
        (since(40.0, key=6), 6, RemoteError("x"), None),
        (since(40.0, key=6), 6, None, None),
        (since(40.0), 6, RemoteTxid(5), since(100.0, 7.0, 6)),  # 키 없는 이전 값은 다시 센다
    ],
)
def test_next_backlog_since(prev, local, remote, expected):
    assert next_backlog_since(prev, local, remote, 100.0, 7.0) == expected


L1 = ("a.ltx", 1)
L2 = ("b.ltx", 2)


@pytest.mark.parametrize(
    ("prev", "files", "expected"),
    [
        (None, None, None),
        (None, FileTimes(None, 5.0, L1), None),  # DB 없음
        (None, FileTimes(5.0, None, None), None),  # L0 없음
        (None, FileTimes(5.0, 5.0, L1), None),  # 같음
        (None, FileTimes(4.0, 5.0, L1), None),  # L0 가 더 새로움(정상)
        (None, FileTimes(6.0, 5.0, L1), since(100.0, 7.0, L1)),  # DB 가 새로워짐: 처음 관측
        (since(40.0, key=L1), FileTimes(9.0, 5.0, L1), since(40.0, key=L1)),  # L0 그대로: 유지
        # L0 가 바뀌면(Litestream 이 진행 중) 새로 센다: 쓰기가 계속되는 DB 의 오탐 방지
        (since(40.0, key=L1), FileTimes(9.0, 8.0, L2), since(100.0, 7.0, L2)),
        (since(40.0, key=L1), FileTimes(8.0, 8.0, L2), None),  # 따라잡음
    ],
)
def test_next_pending_since(prev, files, expected):
    assert next_pending_since(prev, files, 100.0, 7.0) == expected


# --- alias_status ---------------------------------------------------------------------

BOOT_OK = {"state": "match", "action": "proceed", "unknown_at_boot": False}
BOOT_KEEP = {"state": "unknown", "action": "keep_local", "unknown_at_boot": True}


SAME = RemoteTxid(5)
IDLE = FileTimes(10.0, 11.0, L1)
NEWER = FileTimes(12.0, 11.0, L1)  # 보조 근거: DB 가 L0 보다 새로움
W_SYNC = WalEvidence(IN_SYNC, "every -wal commit is within the latest L0", 4152, None, True, "k")
W_PEND = WalEvidence(PENDING, "the -wal has commits after the latest L0", 4152, 8272, True, "k")
W_NONE = WalEvidence(NO_EVIDENCE, "no readable -wal file", 4152)


def sample(
    local=5,
    remote=SAME,
    *,
    at=1000.0,
    wall=None,
    backlog=None,
    pending=None,
    files=IDLE,
    wal=W_SYNC,
    backwards=False,
    boot=None,
    error=None,
    code=None,
):
    return Sample(
        at,
        at if wall is None else wall,
        "/srv/app/app.sqlite3",
        local,
        remote,
        boot,
        None,
        code if error else None,
        error,
        files,
        since(backlog) if backlog is not None else None,
        since(pending, key=L1) if pending is not None else None,
        wal,
        backwards,
    )


@pytest.mark.parametrize(
    ("s", "now", "status", "code", "word"),
    [
        (None, 1000.0, UNKNOWN, "not_checked", "not checked yet"),
        (sample(), 1000.0, CAUGHT_UP, "in_sync", "same TXID"),
        (sample(), 1045.0, CAUGHT_UP, "in_sync", "same TXID"),  # REFRESH×3 = 45 경계는 신선
        (sample(), 1045.1, UNKNOWN, "stale", "stuck"),  # 오래된 결과
        (sample(), 999.0, UNKNOWN, "stale", "stuck"),
        (sample(error="path x", code="path_not_real"), 1000.0, UNKNOWN, "path_not_real", "path x"),
        (sample(boot=BOOT_KEEP), 1000.0, UNKNOWN, "unknown_at_boot", "unknown_at_boot"),
        (sample(boot=BOOT_OK), 1000.0, CAUGHT_UP, "in_sync", "same TXID"),
        (sample(remote=RemoteError("boom")), 1000.0, UNKNOWN, "remote_error", "lookup failed"),
        (sample(remote=RemoteEmpty()), 1000.0, UNKNOWN, "remote_empty", "replica is empty"),
        (sample(local=None), 1000.0, UNKNOWN, "no_local_meta", "local Litestream metadata"),
        (sample(local=4), 1000.0, UNKNOWN, "remote_ahead", "another machine"),
        # 로컬 TXID 앞섬: grace(60) 경계
        (sample(local=6, backlog=1000.0), 1000.0, CAUGHT_UP, "local_ahead_within_grace", "60s"),
        (sample(local=6, backlog=940.1), 1000.0, CAUGHT_UP, "local_ahead_within_grace", "60s"),
        (sample(local=6, backlog=940.0), 1000.0, BACKLOG, "local_ahead", "for 60s"),
        (sample(local=6, backlog=900.0), 1000.0, BACKLOG, "local_ahead", "ahead"),
        # 오래된 결과는 backlog 보다 먼저다
        (sample(local=6, backlog=900.0), 1100.0, UNKNOWN, "stale", "stuck"),
        # 주 근거: WAL 에 L0 뒤 커밋이 있음 — grace(60) 경계. 파일 시각과 무관하다
        (sample(wal=W_PEND, pending=1000.0), 1000.0, CAUGHT_UP, "db_changed_within_grace", "60s"),
        (sample(wal=W_PEND, pending=940.1), 1000.0, CAUGHT_UP, "db_changed_within_grace", "60s"),
        (sample(wal=W_PEND, pending=940.0), 1000.0, BACKLOG, "db_not_replicated", "replicate"),
        (sample(wal=W_PEND, pending=940.0, files=IDLE), 1000.0, BACKLOG, "db_not_replicated", ""),
        # 주 근거 in_sync 면 파일 시각이 새로워도 caught_up(보조는 쓰지 않는다)
        (sample(wal=W_SYNC, files=NEWER), 1000.0, CAUGHT_UP, "in_sync", ""),
        # 둘 다: TXID backlog 가 먼저 나온다
        (
            sample(local=6, backlog=900.0, wal=W_PEND, pending=900.0),
            1000.0,
            BACKLOG,
            "local_ahead",
            "",
        ),
        (
            sample(local=6, backlog=990.0, wal=W_PEND, pending=900.0),
            1000.0,
            BACKLOG,
            "db_not_replicated",
            "",
        ),
        # 근거 없음: 절대 caught_up 이 아니다
        (sample(wal=W_NONE), 1000.0, UNKNOWN, "no_wal_evidence", "no readable -wal"),
        (sample(wal=None), 1000.0, UNKNOWN, "no_wal_evidence", ""),
        (sample(wal=W_NONE, files=NEWER, pending=1000.0), 1000.0, UNKNOWN, "no_wal_evidence", ""),
        (sample(wal=W_NONE, files=NEWER, pending=940.1), 1000.0, UNKNOWN, "no_wal_evidence", ""),
        (
            sample(wal=W_NONE, files=NEWER, pending=940.0),
            1000.0,
            BACKLOG,
            "db_not_replicated",
            "file times only",
        ),
        (sample(wal=W_NONE, local=6, backlog=990.0), 1000.0, UNKNOWN, "no_wal_evidence", ""),
        # 보조 근거의 파일 시각 역행 → unknown(앞선 pending 이 있어도)
        (
            sample(wal=W_NONE, backwards=True, pending=900.0),
            1000.0,
            UNKNOWN,
            "file_time_backwards",
            "backwards",
        ),
        # 주 근거가 있으면 파일 시각 역행과 무관하다
        (
            sample(wal=W_PEND, backwards=True, pending=900.0),
            1000.0,
            BACKLOG,
            "db_not_replicated",
            "",
        ),
        (sample(wal=W_SYNC, backwards=True), 1000.0, CAUGHT_UP, "in_sync", ""),
        # 판정 불가가 pending 보다 먼저다
        (
            sample(remote=RemoteEmpty(), wal=W_PEND, pending=900.0),
            1000.0,
            UNKNOWN,
            "remote_empty",
            "",
        ),
    ],
)
def test_alias_status_table(s, now, status, code, word):
    result = alias_status(s, now, refresh=15, grace=60)
    assert (result["status"], result["code"]) == (status, code), result
    assert word in result["reason"]


def test_alias_status_fields():
    # 지속 시간은 monotonic(observed) 으로, 표시는 벽시계(wall)로 한다
    s = Sample(
        5000.0,
        1000.0,
        "/srv/app/app.sqlite3",
        0x1A,
        RemoteTxid(0x19),
        BOOT_OK,
        None,
        None,
        None,
        FileTimes(1000.25, 999.5, L1),
        Since(4990.0, 990.0),
        Since(4995.0, 995.0, L1),
        W_PEND,
    )
    result = alias_status(s, 5002.5, refresh=15, grace=60)
    assert result == {
        "status": CAUGHT_UP,
        "code": "local_ahead_within_grace",
        "reason": "local is ahead of the replica for 10s, within BACKLOG_GRACE 60s",
        "path": "/srv/app/app.sqlite3",
        "local_txid": "000000000000001a",
        "remote_txid": "0000000000000019",
        "checked_at": "1970-01-01T00:16:40.000Z",
        "age": 2.5,
        "backlog_since": "1970-01-01T00:16:30.000Z",
        "pending_since": "1970-01-01T00:16:35.000Z",
        "db_changed_at": "1970-01-01T00:16:40.250Z",
        "ltx_at": "1970-01-01T00:16:39.500Z",
        "wal": {
            "evidence": "pending",
            "reason": "the -wal has commits after the latest L0",
            "ltx_wal_end": 4152,
            "wal_commit_end": 8272,
        },
        "boot_state": BOOT_OK,
        "boot_state_error": None,
    }
    empty = alias_status(None, 1.0, refresh=15, grace=60)
    assert empty["checked_at"] is None and empty["local_txid"] is None


def test_last_upload_time_is_not_reported():
    result = alias_status(sample(), 1000.0, refresh=15, grace=60)
    assert not any("upload" in key for key in result)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], UNKNOWN),
        ([CAUGHT_UP], CAUGHT_UP),
        ([CAUGHT_UP, BACKLOG], BACKLOG),
        ([BACKLOG, UNKNOWN, CAUGHT_UP], UNKNOWN),
        ([CAUGHT_UP, UNKNOWN], UNKNOWN),
    ],
)
def test_overall_status(statuses, expected):
    assert overall_status(statuses) == expected
    assert overall_status({str(i): {"status": s} for i, s in enumerate(statuses)}) == expected


# --- parse_config ---------------------------------------------------------------------


def test_parse_config_ok():
    config, errors = parse_config(
        {
            "DATABASES": {
                "default": {
                    "litestream_config": "/etc/litestream.yml",
                    "meta_path": "/srv/meta",
                    "litestream": "/usr/bin/litestream",
                }
            },
            "REFRESH": 5,
            "BACKLOG_GRACE": 0,
        },
        {"default": SQLITE},
    )
    assert errors == []
    assert config == HealthConfig(
        (AliasConfig("default", "/etc/litestream.yml", "/srv/meta", "/usr/bin/litestream"),),
        5.0,
        0.0,
    )


def test_parse_config_defaults_and_relative_paths():
    config, errors = parse_config(
        {"DATABASES": {"default": {"litestream_config": "ls.yml"}}}, {"default": SQLITE}
    )
    assert errors == []
    assert config.refresh == 15 and config.backlog_grace == 60
    assert config.databases[0].litestream_config == os.path.join(os.getcwd(), "ls.yml")
    assert config.databases[0].litestream == "litestream"
    assert config.databases[0].meta_path is None


@pytest.mark.parametrize(
    ("raw", "word"),
    [
        ([], "must be a dict"),
        ({}, "non-empty dict"),
        ({"DATABASES": {}}, "non-empty dict"),
        ({"DATABASES": {"default": {"litestream_config": "/x"}}, "EXTRA": 1}, "Unknown key"),
        ({"DATABASES": {"other": {"litestream_config": "/x"}}}, "not in DATABASES"),
        ({"DATABASES": {"pg": {"litestream_config": "/x"}}}, "is not a"),
        ({"DATABASES": {"default": "/x"}}, "must be a dict"),
        ({"DATABASES": {"default": {}}}, "litestream_config"),
        ({"DATABASES": {"default": {"litestream_config": ""}}}, "litestream_config"),
        ({"DATABASES": {"default": {"litestream_config": "/x", "meta": 1}}}, "unknown key"),
        ({"DATABASES": {"default": {"litestream_config": "/x", "meta_path": 3}}}, "meta_path"),
        ({"DATABASES": {"default": {"litestream_config": "/x", "litestream": ""}}}, "litestream"),
        ({"DATABASES": {"default": {"litestream_config": "/x"}}, "REFRESH": 0}, "REFRESH"),
        ({"DATABASES": {"default": {"litestream_config": "/x"}}, "REFRESH": True}, "REFRESH"),
        ({"DATABASES": {"default": {"litestream_config": "/x"}}, "REFRESH": "15"}, "REFRESH"),
        (
            {"DATABASES": {"default": {"litestream_config": "/x"}}, "BACKLOG_GRACE": -1},
            "BACKLOG_GRACE",
        ),
        (
            {"DATABASES": {"default": {"litestream_config": "/x"}}, "BACKLOG_GRACE": float("inf")},
            "BACKLOG_GRACE",
        ),
    ],
)
def test_parse_config_errors(raw, word):
    databases = {"default": SQLITE, "pg": {"ENGINE": "django.db.backends.postgresql"}}
    config, errors = parse_config(raw, databases)
    assert config is None
    assert any(word in message for message, _ in errors), errors


# --- 부팅 상태 파일 --------------------------------------------------------------------


def test_read_boot_state(tmp_path):
    db = tmp_path / "app.sqlite3"
    state = tmp_path / "app.sqlite3.boot-state.json"
    assert "no boot state file" in read_boot_state(str(db))[1]

    body = {
        "version": 1,
        "state": "unknown",
        "action": "keep_local",
        "reason_code": "no_local_meta",
        "reason": "no local meta",
        "unknown_at_boot": True,
        "litestream_version": None,
        "at": "2026-10-08T01:02:03Z",
        "extra": "not copied",
    }
    state.write_text(json.dumps(body))
    loaded, problem = read_boot_state(str(db))
    assert problem is None
    assert loaded == {k: v for k, v in body.items() if k not in ("extra", "version")}

    for bad, word in [
        ("{", "not valid JSON"),
        ("[]", "unexpected format"),
        (json.dumps({**body, "version": 2}), "unexpected format"),
        (json.dumps({**body, "unknown_at_boot": "yes"}), "unknown_at_boot"),
        (json.dumps({**body, "state": 3}), "state"),
    ]:
        state.write_text(bad)
        loaded, problem = read_boot_state(str(db))
        assert loaded is None and word in problem


def test_read_boot_state_rejects_non_regular(tmp_path):
    db = tmp_path / "app.sqlite3"
    (tmp_path / "app.sqlite3.boot-state.json").mkdir()
    loaded, problem = read_boot_state(str(db))
    assert loaded is None and problem


# --- Monitor (가짜 조회) ---------------------------------------------------------------


class Fakes:
    def __init__(self, db: str, local=5, remote=SAME, files=IDLE, wal=W_SYNC):
        self.db = db
        self.local = local
        self.remote = remote
        self.files = files
        self.wal = wal
        self.calls = 0
        self.fail = False

    def remote_fn(self, cfg, path):
        self.calls += 1
        if self.fail:
            raise RuntimeError("probe exploded")
        return self.remote

    def local_fn(self, path, meta):
        return self.local

    def files_fn(self, path, meta):
        return self.files

    def wal_fn(self, path, meta):
        return self.wal

    def path_of(self, alias):
        return self.db, None


def monitor(tmp_path, *, refresh=0.05, grace=60.0, clock=time.time, monotonic=time.monotonic, **kw):
    fakes = Fakes(str(tmp_path / "app.sqlite3"), **kw)
    config = HealthConfig((AliasConfig("default", "/etc/litestream.yml"),), refresh, grace)
    m = Monitor(
        config,
        remote=fakes.remote_fn,
        local=fakes.local_fn,
        files=fakes.files_fn,
        wal=fakes.wal_fn,
        path_of=fakes.path_of,
        clock=clock,
        monotonic=monotonic,
    )
    return m, fakes


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_monitor_is_not_started_until_requested(tmp_path):
    m, fakes = monitor(tmp_path)
    time.sleep(0.2)
    assert not m.running and fakes.calls == 0
    assert m.report()["status"] == UNKNOWN
    assert "not checked yet" in m.report()["databases"]["default"]["reason"]


def test_monitor_refreshes_periodically(tmp_path, caplog):
    m, fakes = monitor(tmp_path)
    m.ensure_started()
    try:
        assert wait_for(lambda: m.refreshes >= 3)
        assert m.report()["status"] == CAUGHT_UP
        fakes.remote = RemoteError("s3 down")
        before = m.refreshes
        assert wait_for(lambda: m.refreshes >= before + 2)
        report = m.report()
        assert report["status"] == UNKNOWN
        assert report["databases"]["default"]["code"] == "remote_error"
        assert "s3 down" not in report["databases"]["default"]["reason"]  # 원문은 로그로만
        logged = [r.getMessage() for r in caplog.records if r.name == health.LOGGER_NAME]
        assert logged == ["health 'default': remote lookup failed: s3 down"]  # 반복하지 않는다
    finally:
        m.stop(5)


def test_monitor_survives_exceptions(tmp_path):
    m, fakes = monitor(tmp_path)
    fakes.fail = True
    m.ensure_started()
    try:
        assert wait_for(lambda: m.refreshes >= 2)
        entry = m.report()["databases"]["default"]
        assert entry["code"] == "refresh_failed" and "RuntimeError" in entry["reason"]
        assert "probe exploded" not in entry["reason"]
        assert m.running
        fakes.fail = False
        before = m.refreshes
        assert wait_for(lambda: m.refreshes >= before + 1)
        assert m.report()["status"] == CAUGHT_UP
    finally:
        m.stop(5)


def test_monitor_survives_clock_and_path_exceptions(tmp_path):
    m, fakes = monitor(tmp_path)

    def broken(alias):
        raise OSError("weird")

    m.path_of = broken
    m.refresh_once()
    assert m.report()["databases"]["default"]["code"] == "refresh_failed"


def test_monitor_restarts_after_pid_change(tmp_path, monkeypatch):
    m, fakes = monitor(tmp_path)
    m.ensure_started()
    first, first_stop = m._thread, m._stop
    try:
        assert wait_for(lambda: m.refreshes >= 1)
        m.ensure_started()
        assert m._thread is first  # 같은 프로세스에서는 다시 시작하지 않는다
        real_pid = os.getpid()
        monkeypatch.setattr(os, "getpid", lambda: real_pid + 1)
        assert not m.running  # 다른 PID(포크 전 부모)의 스레드는 이 프로세스의 것이 아니다
        m.ensure_started()
        assert m._thread is not first and m._thread.is_alive() and m.running
        before = fakes.calls
        assert wait_for(lambda: fakes.calls > before)
    finally:
        m.stop(5)
        first_stop.set()
        first.join(5)


def test_monitor_backlog_with_injected_clock(tmp_path):
    now = [1000.0]
    m, fakes = monitor(
        tmp_path, grace=60.0, refresh=15.0, clock=lambda: now[0], monotonic=lambda: now[0], local=6
    )
    m.refresh_once()
    db = m.report()["databases"]["default"]
    assert db["status"] == CAUGHT_UP and db["backlog_since"] == "1970-01-01T00:16:40.000Z"
    now[0] = 1059.0
    m.refresh_once()
    assert m.report()["status"] == CAUGHT_UP
    now[0] = 1060.0
    m.refresh_once()
    assert m.report()["status"] == BACKLOG
    fakes.remote = RemoteTxid(6)
    now[0] = 1061.0
    m.refresh_once()
    db = m.report()["databases"]["default"]
    assert db["status"] == CAUGHT_UP and db["backlog_since"] is None


def test_monitor_stale_result_is_unknown(tmp_path):
    now = [1000.0]
    m, _ = monitor(tmp_path, refresh=15.0, monotonic=lambda: now[0])
    m.refresh_once()
    assert m.report(now=1045.0)["status"] == CAUGHT_UP
    report = m.report(now=1046.0)
    assert report["status"] == UNKNOWN and "stuck" in report["databases"]["default"]["reason"]


def test_monitor_path_rules(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    m, fakes = monitor(tmp_path)
    fakes.db = str(link / "app.sqlite3")  # 부모가 링크 (D-15)
    m.refresh_once()
    db = m.report()["databases"]["default"]
    assert db["status"] == UNKNOWN and "not a real path" in db["reason"]
    assert db["code"] == "path_not_real"
    assert fakes.calls == 0  # 원격을 부르지 않는다

    (real / "file.sqlite3").write_bytes(b"")
    (real / "app.sqlite3").symlink_to(real / "file.sqlite3")
    fakes.db = str(real / "app.sqlite3")  # DB 자체가 링크
    m.refresh_once()
    assert "symbolic link" in m.report()["databases"]["default"]["reason"]

    fakes.db = str(real / "ok.sqlite3")
    m.config = HealthConfig(
        (AliasConfig("default", "/etc/litestream.yml", str(link / "meta")),), 0.05, 60.0
    )
    m.refresh_once()
    reason = m.report()["databases"]["default"]["reason"]
    assert reason.startswith("meta_path ") and "is not a real path" in reason


def test_monitor_boot_state(tmp_path):
    m, _ = monitor(tmp_path)
    m.refresh_once()
    db = m.report()["databases"]["default"]
    assert db["status"] == CAUGHT_UP  # 상태 파일이 없어도 unknown 으로 바꾸지 않는다
    assert db["boot_state"] is None and "no boot state file" in db["boot_state_error"]
    (tmp_path / "app.sqlite3.boot-state.json").write_text(
        json.dumps({"version": 1, **BOOT_KEEP, "at": "2026-10-08T01:02:03Z"})
    )
    m.refresh_once()
    db = m.report()["databases"]["default"]
    assert db["status"] == UNKNOWN and "unknown_at_boot" in db["reason"]
    assert db["boot_state"]["action"] == "keep_local"


def test_health_module_does_not_start_thread_on_import():
    assert health._monitor is None
    assert not any(t.name == "sqlite-ops-health" for t in threading.enumerate() if t.is_alive())


# --- 뷰 (서브프로세스 Django) ----------------------------------------------------------

VIEW_SCRIPT = """
import json
import os
import sys
import threading
import time

import django
from django.conf import settings

config = json.loads(sys.argv[1])
fake = config.pop("_fake")
settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"],
    USE_TZ=True,
    ROOT_URLCONF="__main__",
    ALLOWED_HOSTS=["testserver"],
    **config,
)
django.setup()

import logging

from django.urls import path

LOG = []


class _Collect(logging.Handler):
    def emit(self, record):
        LOG.append(record.getMessage())


logging.getLogger("django_sqlite_ops.health").addHandler(_Collect())
logging.getLogger("django_sqlite_ops.health").setLevel(logging.DEBUG)

from django_sqlite_ops import health
from django_sqlite_ops.boot.decide import RemoteTxid
from django_sqlite_ops.wal import IN_SYNC, WalEvidence

urlpatterns = [path("healthz", health.health_view)]

from django.db import connections
from django.test import Client

out = {}
from django.core.checks import run_checks

run_checks()
out["threads_after_setup"] = [t.name for t in threading.enumerate()]

if fake is not None:
    cfg, errors = health.load_config()
    health._monitor = health.Monitor(
        cfg,
        remote=lambda c, p: RemoteTxid(fake["remote"]),
        local=lambda p, m: fake["local"],
        wal=lambda p, m: WalEvidence(IN_SYNC, "every -wal commit is within the latest L0"),
    )

client = Client()
responses = []
for query in ["", "?strict=1"]:
    r = client.get("/healthz" + query)
    responses.append({"code": r.status_code, "body": r.json(), "cc": r["Cache-Control"]})
out["first"] = responses
m = health._monitor
if m is not None:
    deadline = time.monotonic() + 20
    while m.refreshes < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
responses = []
for query in ["", "?strict=1"]:
    r = client.get("/healthz" + query)
    responses.append({"code": r.status_code, "body": r.json()})
out["second"] = responses
out["threads"] = [t.name for t in threading.enumerate()]
out["connections"] = {a: connections[a].connection is None for a in connections}
out["db_exists"] = {
    a: os.path.exists(c["NAME"]) for a, c in settings.DATABASES.items()
}
out["log"] = LOG
print(json.dumps(out))
"""


def run_view(databases, *, fake=None, **extra):
    config = {"DATABASES": databases, "_fake": fake, **extra}
    result = subprocess.run(
        [sys.executable, "-c", VIEW_SCRIPT, json.dumps(config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def health_settings(config_path, **extra):
    return {
        "HEALTH": {
            "DATABASES": {"default": {"litestream_config": str(config_path)}},
            "REFRESH": 0.05,
            **extra,
        }
    }


def test_view_caught_up_and_strict(tmp_path):
    db = tmp_path / "app.sqlite3"
    out = run_view(
        {"default": {**SQLITE, "NAME": str(db)}},
        fake={"local": 7, "remote": 7},
        SQLITE_OPS=health_settings(tmp_path / "litestream.yml"),
    )
    # 첫 요청은 스레드를 시작할 뿐 결과를 기다리지 않는다
    assert "sqlite-ops-health" not in out["threads_after_setup"]
    first, first_strict = out["first"]
    assert first["code"] == 200 and first["cc"] == "no-store"
    assert first["body"]["status"] == UNKNOWN
    assert first_strict["code"] == 503
    second, second_strict = out["second"]
    body = second["body"]
    assert second["code"] == 200 and second_strict["code"] == 200
    assert body["version"] == 1 and body["status"] == CAUGHT_UP
    assert set(body) == {"version", "status", "refresh", "backlog_grace", "databases"}
    entry = body["databases"]["default"]
    assert set(entry) == {
        "status",
        "code",
        "reason",
        "path",
        "local_txid",
        "remote_txid",
        "checked_at",
        "age",
        "backlog_since",
        "pending_since",
        "db_changed_at",
        "ltx_at",
        "wal",
        "boot_state",
        "boot_state_error",
    }
    assert entry["code"] == "in_sync"
    assert entry["path"] == str(db) and entry["local_txid"] == "0000000000000007"
    assert "sqlite-ops-health" in out["threads"]
    # DB 연결을 열지 않았고 DB 파일도 생기지 않았다
    assert out["connections"] == {"default": True}
    assert out["db_exists"] == {"default": False}


def test_view_real_lookup_failure_is_unknown_with_200(tmp_path):
    db = tmp_path / "app.sqlite3"
    out = run_view(
        {"default": {**SQLITE, "NAME": str(db)}},
        SQLITE_OPS=health_settings(
            tmp_path / "missing.yml",
            DATABASES={
                "default": {"litestream_config": "/nope.yml", "litestream": "/nonexistent/ls"}
            },
        ),
    )
    second, second_strict = out["second"]
    assert second["code"] == 200 and second["body"]["status"] == UNKNOWN
    assert second_strict["code"] == 503
    assert "remote lookup failed" in second["body"]["databases"]["default"]["reason"]
    assert out["connections"] == {"default": True}
    assert out["db_exists"] == {"default": False}


def test_view_not_a_file_database_is_unknown(tmp_path):
    out = run_view(
        {"default": {**SQLITE, "NAME": ":memory:"}},
        SQLITE_OPS=health_settings(tmp_path / "litestream.yml"),
    )
    reason = out["second"][0]["body"]["databases"]["default"]["reason"]
    assert "not a writable file database" in reason


def test_view_without_health_settings(tmp_path):
    out = run_view({"default": {**SQLITE, "NAME": str(tmp_path / "app.sqlite3")}})
    first = out["first"][0]
    assert first["code"] == 200
    assert first["body"] == {
        "version": 1,
        "status": UNKNOWN,
        "code": "not_configured",
        "reason": "SQLITE_OPS['HEALTH'] is not configured",
        "databases": {},
    }
    assert "sqlite-ops-health" not in out["threads"]


def test_view_invalid_health_settings(tmp_path):
    out = run_view(
        {"default": {**SQLITE, "NAME": str(tmp_path / "app.sqlite3")}},
        SQLITE_OPS={"HEALTH": {"DATABASES": {}}},
    )
    body = out["first"][0]["body"]
    assert body["status"] == UNKNOWN and "E002" in body["reason"]


# --- E002 -----------------------------------------------------------------------------

CHECK_SCRIPT = """
import json
import sys

import django
from django.conf import settings

config = json.loads(sys.argv[1])
settings.configure(INSTALLED_APPS=["django_sqlite_ops"], USE_TZ=True, **config)
django.setup()

from django.core.checks import run_checks

messages = run_checks()
print(json.dumps(sorted([m.id, m.obj or ""] for m in messages if m.id.startswith("sqlite_ops."))))
"""


def run_checks(**config):
    result = subprocess.run(
        [sys.executable, "-c", CHECK_SCRIPT, json.dumps(config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [tuple(x) for x in json.loads(result.stdout)]


@pytest.mark.parametrize("asgi", [None, "proj.asgi.application"])
def test_e002(asgi):
    extra = {"ASGI_APPLICATION": asgi} if asgi else {}
    databases = {"default": SQLITE}
    assert run_checks(DATABASES=databases, **extra) == []
    ok = {"HEALTH": {"DATABASES": {"default": {"litestream_config": "/etc/litestream.yml"}}}}
    assert run_checks(DATABASES=databases, SQLITE_OPS=ok, **extra) == []
    bad = {"HEALTH": {"DATABASES": {"other": {}}, "REFRESH": -1}}
    assert run_checks(DATABASES=databases, SQLITE_OPS=bad, **extra) == [
        ("sqlite_ops.E002", ""),
        ("sqlite_ops.E002", "other"),
        ("sqlite_ops.E002", "other"),
    ]


# --- 실제 Litestream (file://) ---------------------------------------------------------


def _write(db: Path, value: int) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
        conn.execute("INSERT INTO t VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _config(lab: Path, name: str, db: Path, replica: Path) -> Path:
    config = lab / f"{name}.yml"
    config.write_text(
        f"dbs:\n  - path: {db}\n    replica:\n      type: file\n      path: {replica}\n"
    )
    return config


def _replicate_once(binary: str, config: Path) -> None:
    result = subprocess.run(
        [binary, "replicate", "-once", "-config", str(config)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@needs_litestream
def test_real_litestream_states(tmp_path, litestream_binary):
    """caught_up → (복제가 다른 곳으로만 감) backlog → 빈 복제본·조회 실패 unknown."""
    lab = tmp_path / "lab"
    lab.mkdir()
    db = lab / "app.sqlite3"
    replica = lab / "replica"
    config = _config(lab, "a", db, replica)
    # 같은 DB 를 다른 복제본으로 보내는 설정. 로컬 메타는 앞서고 health 의 복제본은 멈춘다.
    elsewhere = _config(lab, "b", db, lab / "elsewhere")

    now = [1000.0]
    cfg = HealthConfig((AliasConfig("default", str(config), None, litestream_binary),), 15.0, 60.0)
    # TXID 비교만 본다. replicate -once 는 매번 끝나며 체크포인트하므로 파일 시각 근거(프로세스
    # 정지)는 여기서 끈다. 그 근거는 아래 라운드 1 회귀 테스트들이 실제 바이너리로 본다.
    m = Monitor(
        cfg,
        path_of=lambda alias: (str(db), None),
        monotonic=lambda: now[0],
        files=lambda db, meta: None,
    )

    _write(db, 1)
    _replicate_once(litestream_binary, config)
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == CAUGHT_UP, entry
    assert entry["local_txid"] == entry["remote_txid"] is not None

    _write(db, 2)
    _replicate_once(litestream_binary, elsewhere)
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == CAUGHT_UP and entry["backlog_since"] is not None, entry
    assert int(entry["local_txid"], 16) > int(entry["remote_txid"], 16)
    now[0] = 1060.0
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == BACKLOG, entry

    # 복제가 다시 health 의 복제본으로 가면 따라잡는다
    _replicate_once(litestream_binary, config)
    now[0] = 1061.0
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == CAUGHT_UP and entry["backlog_since"] is None, entry

    # 복제본 경로가 없음(경로 오타와 같다) → 빈 목록 → unknown
    empty = _config(lab, "c", db, lab / "no-such-replica")
    m.config = HealthConfig(
        (AliasConfig("default", str(empty), None, litestream_binary),), 15.0, 60.0
    )
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == UNKNOWN and "replica is empty" in entry["reason"], entry

    # 설정 파일을 읽을 수 없음 → 조회 실패 → unknown
    m.config = HealthConfig(
        (AliasConfig("default", str(lab / "missing.yml"), None, litestream_binary),), 15.0, 60.0
    )
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == UNKNOWN and "remote lookup failed" in entry["reason"], entry


# --- 라운드 1 회귀 (review-1) ---------------------------------------------------------


def test_view_is_outside_atomic_requests_for_every_alias(tmp_path):
    """review-1 P2: ATOMIC_REQUESTS 가 켜진 별칭이 있어도 헬스 요청은 연결·파일을 만들지 않는다."""
    databases = {
        "default": {**SQLITE, "NAME": str(tmp_path / "a.sqlite3"), "ATOMIC_REQUESTS": True},
        "other": {**SQLITE, "NAME": str(tmp_path / "b.sqlite3"), "ATOMIC_REQUESTS": True},
    }
    out = run_view(
        databases, fake={"local": 1, "remote": 1}, SQLITE_OPS=health_settings(tmp_path / "x.yml")
    )
    assert out["second"][0]["code"] == 200
    assert out["connections"] == {"default": True, "other": True}
    assert out["db_exists"] == {"default": False, "other": False}


@needs_litestream
def test_remote_error_text_is_not_in_response_or_log(tmp_path, litestream_binary):
    """review-1 P2: Litestream stderr 의 자격 증명이 응답·로그에 나오지 않는다."""
    db = tmp_path / "app.sqlite3"
    config = tmp_path / "bad.yml"
    config.write_text(
        f"dbs:\n  - path: {db}\n    replica:\n      type: s3\n      bucket: dummy\n"
        '      endpoint: "http://user:FAKE_SECRET@127.0.0.1:1/%zz"\n'
        "      access-key-id: dummy\n      secret-access-key: dummy\n"
    )
    out = run_view(
        {"default": {**SQLITE, "NAME": str(db)}},
        SQLITE_OPS={
            "HEALTH": {
                "DATABASES": {
                    "default": {"litestream_config": str(config), "litestream": litestream_binary}
                },
                "REFRESH": 0.05,
            }
        },
    )
    body = out["second"][0]["body"]
    entry = body["databases"]["default"]
    assert entry["status"] == UNKNOWN and entry["code"] == "remote_error"
    assert "FAKE_SECRET" not in json.dumps(out["second"])
    assert out["log"], "the raw error should be logged (redacted)"
    assert "FAKE_SECRET" not in json.dumps(out["log"])
    assert any("remote lookup failed" in line for line in out["log"])


def test_backlog_survives_wall_clock_going_backwards(tmp_path):
    """review-1 P2: 벽시계 1000 → 1060 → 900 이어도 monotonic 으로 잰 backlog 는 유지된다."""
    wall = [1000.0]
    mono = [5000.0]
    m, _ = monitor(
        tmp_path,
        refresh=15.0,
        grace=60.0,
        clock=lambda: wall[0],
        monotonic=lambda: mono[0],
        local=2,
        remote=RemoteTxid(1),
    )
    for w, t, expected in [
        (1000.0, 5000.0, CAUGHT_UP),
        (1060.0, 5060.0, BACKLOG),
        (900.0, 5075.0, BACKLOG),
    ]:
        wall[0], mono[0] = w, t
        m.refresh_once()
        assert m.report()["status"] == expected, (w, m.report())


def test_meta_path_dotdot_is_not_folded(tmp_path):
    """review-1 P2: meta_path 의 '..' 를 접지 않고 D-15 로 검사한다."""
    base = tmp_path / "paths"
    (base / "elsewhere" / "inner").mkdir(parents=True)
    (base / "link").symlink_to(base / "elsewhere" / "inner")
    (base / "meta").mkdir()
    raw = str(base / "link" / ".." / "meta")
    config, errors = parse_config(
        {"DATABASES": {"default": {"litestream_config": "/x.yml", "meta_path": raw}}},
        {"default": SQLITE},
    )
    assert errors == []
    assert config.databases[0].meta_path == raw
    m, fakes = monitor(tmp_path)
    m.config = config
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == UNKNOWN and entry["code"] == "path_not_real", entry
    assert fakes.calls == 0


@needs_litestream
def test_real_stopped_replicate_once_then_write_is_backlog(tmp_path, litestream_binary):
    """review-1 P2 재현: replicate -once 뒤 프로세스 없이 커밋하면 backlog 다(grace 0)."""
    lab = tmp_path / "lab"
    lab.mkdir()
    db = lab / "stopped.sqlite3"
    config = _config(lab, "a", db, lab / "replica")
    _write(db, 1)
    _replicate_once(litestream_binary, config)
    cfg = HealthConfig((AliasConfig("default", str(config), None, litestream_binary),), 15.0, 0.0)
    m = Monitor(cfg, path_of=lambda alias: (str(db), None))
    m.refresh_once()
    # replicate 가 끝날 때 마지막 연결을 닫으며 체크포인트해 DB mtime 이 L0 보다 늦다(실측).
    # 프로세스가 없으므로 이것만으로도 grace 뒤 backlog 다(grace 0 이라 바로).
    assert db.stat().st_mtime > m.samples["default"].files.ltx_at
    assert m.report()["databases"]["default"]["local_txid"] is not None
    _write(db, 2)
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == BACKLOG and entry["code"] == "db_not_replicated", entry
    assert entry["local_txid"] == entry["remote_txid"]  # TXID 만으로는 보이지 않는 경우다


@needs_litestream
def test_real_replicate_stopped_then_write_is_backlog_after_grace(tmp_path, litestream_binary):
    """replicate(지속 실행) 중 caught_up, 쓰기 없는 동안 계속 caught_up, 프로세스 종료 뒤 쓰기 →
    grace 안에서는 caught_up(pending_since), grace 뒤 backlog."""
    lab = tmp_path / "lab"
    lab.mkdir()
    db = lab / "app.sqlite3"
    config = _config(lab, "a", db, lab / "replica")
    _write(db, 1)
    mono = [0.0]
    cfg = HealthConfig((AliasConfig("default", str(config), None, litestream_binary),), 15.0, 30.0)
    m = Monitor(cfg, path_of=lambda alias: (str(db), None), monotonic=lambda: mono[0])
    proc = subprocess.Popen(
        [litestream_binary, "replicate", "-config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:

        def synced():
            m.refresh_once()
            entry = m.report()["databases"]["default"]
            return entry["status"] == CAUGHT_UP and entry["code"] == "in_sync"

        assert wait_for(synced, timeout=30), m.report()
        # 쓰기 없는 정상 DB: 시각이 흘러도 caught_up 이다
        for _ in range(3):
            time.sleep(1.1)
            mono[0] += 100.0
            m.refresh_once()
            entry = m.report()["databases"]["default"]
            assert entry["status"] == CAUGHT_UP and entry["code"] == "in_sync", entry
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    _write(db, 2)  # 연결을 닫으면 이 빌드의 SQLite 는 -wal 을 지운다 → WAL 근거 없음
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    # -wal 이 지워졌으면 근거가 없어 unknown(caught_up 으로 단정하지 않는다. 보조 근거는 grace 뒤
    # backlog 로만). -wal 을 남기는 빌드면 L0 뒤 커밋이 보여 grace 안 caught_up 이다.
    assert (entry["status"], entry["code"]) in {
        (UNKNOWN, "no_wal_evidence"),
        (CAUGHT_UP, "db_changed_within_grace"),
    }, entry
    assert entry["pending_since"] is not None
    mono[0] += 30.0
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == BACKLOG and entry["code"] == "db_not_replicated", entry
    assert "replicate process running" in entry["reason"]


@pytest.mark.parametrize(
    ("text", "hidden", "kept"),
    [
        (
            "Custom endpoint `http://user:FAKE_SECRET@127.0.0.1:1/%zz` bad",
            "FAKE_SECRET",
            "127.0.0.1",
        ),
        ("s3://AKIA:FAKE_SECRET@bucket/db", "FAKE_SECRET", "bucket/db"),
        ("GET https://b/x?X-Amz-Signature=FAKE_SECRET&part=2", "FAKE_SECRET", "part=2"),
        ("https://b/x?a=1&token=FAKE_SECRET", "FAKE_SECRET", "a=1"),
        ("secret-access-key: FAKE_SECRET", "FAKE_SECRET", "secret-access-key"),
        ('password="FAKE SECRET"', "FAKE SECRET", "password"),
        ("LITESTREAM_ACCESS_KEY_ID=FAKE_SECRET", "FAKE_SECRET", "LITESTREAM_ACCESS_KEY_ID"),
        ("unknown replica type in config", "", "unknown replica type in config"),
    ],
)
def test_redact(text, hidden, kept):
    out = redact(text)
    assert kept in out
    if hidden:
        assert hidden not in out


def test_boot_state_strings_are_redacted(tmp_path):
    db = tmp_path / "app.sqlite3"
    (tmp_path / "app.sqlite3.boot-state.json").write_text(
        json.dumps({**BOOT_OK, "version": 1, "reason": "via http://u:FAKE_SECRET@h/"})
    )
    loaded, problem = read_boot_state(str(db))
    assert problem is None and "FAKE_SECRET" not in loaded["reason"]


def test_relative_meta_path_keeps_dotdot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, errors = parse_config(
        {"DATABASES": {"default": {"litestream_config": "ls.yml", "meta_path": "a/../meta"}}},
        {"default": SQLITE},
    )
    assert errors == []
    assert config.databases[0].meta_path == os.path.join(os.getcwd(), "a/../meta")


# --- 라운드 2 회귀 (review-2): WAL 위치 근거 ------------------------------------------


def test_next_wal_pending_since():
    p1 = WalEvidence(PENDING, "", 4152, 8272, True, "k1")
    p2 = WalEvidence(PENDING, "", 8272, 12392, True, "k2")
    assert next_wal_pending_since(None, p1, 100.0, 7.0) == Since(100.0, 7.0, ("wal", "k1"))
    kept = Since(40.0, 1.0, ("wal", "k1"))
    assert next_wal_pending_since(kept, p1, 100.0, 7.0) is kept  # 같은 L0: 유지
    assert next_wal_pending_since(kept, p2, 100.0, 7.0) == Since(100.0, 7.0, ("wal", "k2"))
    assert next_wal_pending_since(kept, W_SYNC, 100.0, 7.0) is None
    assert next_wal_pending_since(kept, W_NONE, 100.0, 7.0) is None
    assert next_wal_pending_since(kept, None, 100.0, 7.0) is None


@pytest.mark.parametrize(
    ("prev", "cur", "expected"),
    [
        (None, FileTimes(1.0, 1.0, L1, 1.0, 1.0), False),
        (FileTimes(5.0, 5.0, L1, 5.0, 5.0), FileTimes(6.0, 5.0, L1, 6.0, 5.0), False),
        (FileTimes(5.0, 5.0, L1, 5.0, 5.0), FileTimes(5.0, 5.0, L1, 4.0, 5.0), True),  # DB
        (FileTimes(5.0, 5.0, L1, 5.0, 5.0), FileTimes(5.0, 5.0, L1, 5.0, 4.0), True),  # WAL
        (FileTimes(5.0, 5.0, L1, 5.0, 5.0), FileTimes(5.0, 4.0, L2, 5.0, 5.0), True),  # L0
        # -wal 이 지워졌다 생기는 것은 역행이 아니다
        (FileTimes(9.0, 5.0, L1, 5.0, 9.0), FileTimes(5.0, 5.0, L1, 5.0, None), False),
        (FileTimes(5.0, 5.0, L1, 5.0, None), FileTimes(5.0, 5.0, L1, 5.0, 1.0), False),
    ],
)
def test_files_went_backwards(prev, cur, expected):
    assert files_went_backwards(prev, cur) is expected


class WalLab:
    """실제 SQLite 가 쓴 -wal 바이트를 만든다. 읽는 연결을 열어 두어 -wal 이 지워지지 않게 한다."""

    def __init__(self, tmp_path: Path):
        self.db = tmp_path / "w.sqlite3"
        self.wal = Path(str(self.db) + "-wal")
        self.writer = sqlite3.connect(self.db, isolation_level=None)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute("CREATE TABLE t (x)")
        self.holder = sqlite3.connect(self.db)
        self.holder.execute("SELECT 1").fetchall()

    def commit(self, n=1):
        for _ in range(n):
            self.writer.execute("INSERT INTO t VALUES (randomblob(100))")
        return self.wal.stat().st_size

    def header(self):
        raw = self.wal.read_bytes()[:32]
        return struct.unpack(">8I", raw)

    def ltx(self, end, *, salts=None, page_size=None):
        magic, _v, ps, _seq, s1, s2, _c1, _c2 = self.header()
        if salts is not None:
            s1, s2 = salts
        return LtxWalRange(page_size or ps, WAL_HEADER, end - WAL_HEADER, s1, s2)

    def close(self):
        self.holder.close()
        self.writer.close()


WAL_HEADER = 32


@pytest.fixture
def wal_lab(tmp_path):
    lab = WalLab(tmp_path)
    yield lab
    lab.close()


def test_wal_compare_same_salt(wal_lab):
    end0 = wal_lab.wal.stat().st_size  # CREATE TABLE 커밋
    end1 = wal_lab.commit()
    ev = wal_compare(str(wal_lab.wal), wal_lab.ltx(end1))
    assert (ev.state, ev.salt_match, ev.ltx_end) == (IN_SYNC, True, end1)
    end2 = wal_lab.commit()
    ev = wal_compare(str(wal_lab.wal), wal_lab.ltx(end1))
    assert (ev.state, ev.commit_end) == (PENDING, end2)
    # L0 가 아무 프레임도 담지 않았으면(끝 = WAL 헤더) 첫 커밋부터 pending
    ev = wal_compare(str(wal_lab.wal), wal_lab.ltx(WAL_HEADER))
    assert (ev.state, ev.commit_end) == (PENDING, end0)
    assert wal_compare(str(wal_lab.wal), wal_lab.ltx(end2)).state == IN_SYNC


def test_wal_compare_header_only_is_in_sync_for_empty_l0(tmp_path):
    lab = WalLab(tmp_path)
    try:
        # CREATE TABLE 커밋을 담은 L0
        end = lab.wal.stat().st_size
        assert wal_compare(str(lab.wal), lab.ltx(end)).state == IN_SYNC
    finally:
        lab.close()


def test_wal_compare_invalid_frames_end_the_wal(wal_lab, tmp_path):
    end1 = wal_lab.commit()
    end2 = wal_lab.commit()
    raw = bytearray(wal_lab.wal.read_bytes())
    copy = tmp_path / "copy-wal"
    # L0 뒤 첫 프레임의 체크섬이 틀리면 그 프레임부터 무효 → L0 뒤 커밋 없음
    bad = bytearray(raw)
    bad[end1 + 16] ^= 0xFF
    copy.write_bytes(bytes(bad))
    assert wal_compare(str(copy), wal_lab.ltx(end1)).state == IN_SYNC
    # 페이지 데이터가 덜 쓰인 커밋 프레임(파일이 프레임 중간에서 끝남)은 커밋이 아니다
    copy.write_bytes(bytes(raw[: end2 - 10]))
    assert wal_compare(str(copy), wal_lab.ltx(end1)).state == IN_SYNC
    # 다른 salt 의 프레임(이전 세대)은 무효
    other = bytearray(raw)
    other[end1 + 8] ^= 0xFF
    copy.write_bytes(bytes(other))
    assert wal_compare(str(copy), wal_lab.ltx(end1)).state == IN_SYNC


def test_wal_compare_restarted_wal(wal_lab):
    end1 = wal_lab.commit()
    old = wal_lab.ltx(end1)
    wal_lab.holder.close()
    wal_lab.writer.execute("PRAGMA wal_checkpoint(RESTART)").fetchall()
    wal_lab.holder = sqlite3.connect(wal_lab.db)
    wal_lab.holder.execute("SELECT 1").fetchall()
    wal_lab.commit()  # 새 salt 로 다시 시작된 WAL 에 커밋
    assert (wal_lab.header()[4], wal_lab.header()[5]) != (old.salt1, old.salt2)
    ev = wal_compare(str(wal_lab.wal), old)
    assert (ev.state, ev.salt_match) == (PENDING, False)


def test_wal_compare_restarted_wal_without_commit_is_no_evidence(wal_lab, tmp_path):
    end1 = wal_lab.commit()
    raw = bytearray(wal_lab.wal.read_bytes())
    # 헤더 salt 만 바꾸고 헤더 체크섬을 다시 계산한다: 기존 프레임은 모두 이전 세대가 된다
    magic = struct.unpack(">I", raw[:4])[0]
    struct.pack_into(">I", raw, 16, (struct.unpack(">I", raw[16:20])[0] + 1) & 0xFFFFFFFF)
    c = wal_checksum(magic == 0x377F0682, 0, 0, bytes(raw[:24]))
    struct.pack_into(">II", raw, 24, *c)
    copy = tmp_path / "copy-wal"
    copy.write_bytes(bytes(raw))
    ev = wal_compare(str(copy), wal_lab.ltx(end1))
    assert (ev.state, ev.salt_match) == (NO_EVIDENCE, False)


def test_wal_compare_no_evidence_cases(wal_lab, tmp_path):
    end1 = wal_lab.commit()
    wal_lab.commit()
    wal = str(wal_lab.wal)
    raw = wal_lab.wal.read_bytes()
    assert wal_compare(wal, None).state == NO_EVIDENCE
    assert wal_compare(str(tmp_path / "missing-wal"), wal_lab.ltx(end1)).state == NO_EVIDENCE
    empty = tmp_path / "empty-wal"
    empty.write_bytes(b"")
    assert wal_compare(str(empty), wal_lab.ltx(end1)).state == NO_EVIDENCE
    badhdr = tmp_path / "badhdr-wal"
    badhdr.write_bytes(raw[:24] + bytes(8) + raw[32:])  # 헤더 체크섬 틀림
    assert wal_compare(str(badhdr), wal_lab.ltx(end1)).state == NO_EVIDENCE
    assert wal_compare(wal, wal_lab.ltx(end1 + 1)).state == NO_EVIDENCE  # 프레임 경계 아님
    assert wal_compare(wal, wal_lab.ltx(end1, page_size=8192)).state == NO_EVIDENCE
    short = tmp_path / "short-wal"
    short.write_bytes(raw[: end1 - 100])
    assert wal_compare(str(short), wal_lab.ltx(end1)).state == NO_EVIDENCE  # L0 끝보다 짧음
    frame = wal_lab.header()[2] + 24
    prev = bytearray(raw)
    prev[end1 - frame + 8] ^= 0xFF  # L0 끝 직전 프레임의 salt 가 다름(덮어써짐)
    overwritten = tmp_path / "prev-wal"
    overwritten.write_bytes(bytes(prev))
    ev = wal_compare(str(overwritten), wal_lab.ltx(end1))
    assert (ev.state, ev.reason) == (
        NO_EVIDENCE,
        "the -wal frame before the L0 WAL end was overwritten",
    )
    # 스캔 한도
    assert wal_compare(wal, wal_lab.ltx(end1), limit=10).state == NO_EVIDENCE
    # 링크·FIFO 는 따라가거나 기다리지 않는다
    link = tmp_path / "link-wal"
    link.symlink_to(wal_lab.wal)
    assert wal_compare(str(link), wal_lab.ltx(end1)).state == NO_EVIDENCE
    fifo = tmp_path / "fifo-wal"
    os.mkfifo(fifo)
    assert wal_compare(str(fifo), wal_lab.ltx(end1)).state == NO_EVIDENCE


def test_ltx_wal_range_header_fields(tmp_path):
    header = bytearray(100)
    header[0:4] = b"LTX1"
    struct.pack_into(">III", header, 4, 2, 4096, 3)
    struct.pack_into(">QQ", header, 16, 1, 1)
    struct.pack_into(">qqII", header, 48, 4152, 4120, 0xDEADBEEF, 7)
    path = tmp_path / "0000000000000001-0000000000000001.ltx"
    path.write_bytes(bytes(header))
    assert ltx_wal_range(path) == LtxWalRange(4096, 4152, 4120, 0xDEADBEEF, 7)
    assert ltx_wal_range(path).end == 8272
    struct.pack_into(">q", header, 48, 0)  # 저널 모드·압축 파일: WAL 정보 없음
    path.write_bytes(bytes(header))
    assert ltx_wal_range(path) is None
    assert ltx_wal_range(tmp_path / "missing.ltx") is None


@needs_litestream
def test_real_l0_records_wal_position(tmp_path, litestream_binary):
    """0.5.17 의 실제 L0 헤더에 WAL 위치·salt 가 있고 현재 -wal 의 커밋 끝과 맞는다."""
    db = tmp_path / "app.sqlite3"
    config = _config(tmp_path, "a", db, tmp_path / "replica")
    holder = sqlite3.connect(db)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE t (x)")
        holder.execute("INSERT INTO t VALUES (1)")
        holder.commit()
        _replicate_once(litestream_binary, config)
        found = local_max_ltx(str(db))
        rng = ltx_wal_range(found[1])
        header = read_wal_header(str(db) + "-wal")
        assert rng is not None and header is not None
        assert (rng.salt1, rng.salt2) == (header.salt1, header.salt2)
        assert rng.page_size == header.page_size
        assert wal_compare(str(db) + "-wal", rng).state == IN_SYNC
        holder.execute("INSERT INTO t VALUES (2)")
        holder.commit()
        ev = wal_compare(str(db) + "-wal", rng)
        assert ev.state == PENDING and ev.commit_end > rng.end
    finally:
        holder.close()


def _race_once(lab: Path, binary: str):
    """8 × 1MiB 행 DB 에서 replicate 를 시작하고 첫 L0 를 쓰는 중에 한 행을 커밋한 뒤, 첫 업로드
    직후 SIGSTOP. ``((원본 행 수, 복원본 행 수), 헬스 항목)``."""
    db = lab / "app.sqlite3"
    config = _config(lab, "a", db, lab / "replica")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x BLOB)")
    conn.executemany("INSERT INTO t (x) VALUES (?)", [(bytes(1024 * 1024),)] * 8)
    conn.commit()
    proc = subprocess.Popen(
        [binary, "replicate", "-config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        l0 = lab / ".app.sqlite3-litestream" / "ltx" / "0"
        assert wait_for(lambda: l0.exists() and any(l0.iterdir()), timeout=20)
        conn.execute("INSERT INTO t (x) VALUES (?)", (b"AFTER_SNAPSHOT_START",))
        conn.commit()

        def uploaded():
            remote = litestream.remote_max_txid(str(db), config=str(config), binary=binary)
            return getattr(remote, "txid", 0) > 0

        assert wait_for(uploaded, timeout=20)
        os.kill(proc.pid, signal.SIGSTOP)
        cfg = HealthConfig((AliasConfig("default", str(config), None, binary),), 15.0, 0.0)
        m = Monitor(cfg, path_of=lambda alias: (str(db), None))
        m.refresh_once()
        entry = m.report()["databases"]["default"]
        restored = lab / "restore.sqlite3"
        result = litestream.restore(str(db), str(restored), config=str(config), binary=binary)
        assert result.ok, result
        rc = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
        try:
            rows = (
                conn.execute("SELECT count(*) FROM t").fetchone()[0],
                rc.execute("SELECT count(*) FROM t").fetchone()[0],
            )
        finally:
            rc.close()
        return rows, entry
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGCONT)
            proc.terminate()
            proc.wait(timeout=30)
        conn.close()


@needs_litestream
def test_real_commit_during_snapshot_then_stop_is_not_in_sync(tmp_path, litestream_binary):
    """review-2 race.py: L0 를 쓰는 중에 커밋하고 첫 업로드 직후 Litestream 을 SIGSTOP.

    복원본에는 그 커밋이 없다. 파일 시각으로는 L0 가 더 새롭지만 헬스는 in_sync 가 아니다.
    """
    seen = []
    for attempt in range(3):
        lab = tmp_path / str(attempt)
        lab.mkdir()
        rows, entry = _race_once(lab, litestream_binary)
        seen.append((rows, entry["code"]))
        assert entry["code"] != "in_sync", entry
        if rows == (9, 8):  # 경쟁이 재현된 경우: 커밋이 복원본에 없다
            assert entry["status"] == BACKLOG and entry["code"] == "db_not_replicated", entry
            assert entry["wal"]["evidence"] == "pending", entry
    assert any(rows == (9, 8) for rows, _ in seen), seen  # 적어도 한 번은 경쟁을 재현했다


@needs_litestream
def test_real_file_time_backwards_is_unknown(tmp_path, litestream_binary):
    """review-2 edge.py: 미복제 쓰기 뒤 파일 시각이 L0 보다 120초 이전으로 가도 in_sync 아님."""
    db = tmp_path / "app.sqlite3"
    config = _config(tmp_path, "a", db, tmp_path / "replica")
    _write(db, 1)
    _replicate_once(litestream_binary, config)
    cfg = HealthConfig((AliasConfig("default", str(config), None, litestream_binary),), 15.0, 0.0)
    m = Monitor(cfg, path_of=lambda alias: (str(db), None))
    _write(db, 2)
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["status"] == BACKLOG and entry["code"] == "db_not_replicated", entry
    ltx_at = m.samples["default"].files.ltx_at
    for path in (db, Path(str(db) + "-wal")):
        if path.exists():
            os.utime(path, (ltx_at - 120, ltx_at - 120))
    m.refresh_once()
    entry = m.report()["databases"]["default"]
    assert entry["code"] != "in_sync" and entry["status"] != CAUGHT_UP, entry
    if m.samples["default"].wal.state == NO_EVIDENCE:
        assert entry["code"] == "file_time_backwards", entry
    m.refresh_once()  # 역행 뒤 다음 관측도 caught_up 으로 돌아가지 않는다
    entry = m.report()["databases"]["default"]
    assert entry["code"] != "in_sync" and entry["status"] != CAUGHT_UP, entry


def test_busy_db_with_progressing_upload_is_not_backlog(tmp_path):
    """매 관측마다 로컬이 한 걸음 앞서도 업로드가 따라오면 backlog 가 아니다(실측한 오탐).

    업로드가 멈추면 그때부터 센다.
    """
    mono = [0.0]
    m, fakes = monitor(tmp_path, refresh=15.0, grace=15.0, monotonic=lambda: mono[0])
    for i in range(10):
        fakes.local, fakes.remote = 10 + i, RemoteTxid(9 + i)
        m.refresh_once()
        assert m.report()["status"] == CAUGHT_UP, (i, m.report())
        mono[0] += 10.0
    # 업로드가 멈춘다(원격 18 고정). 마지막 관측(t=90)의 로컬 19 가 아직 안 올라갔으므로 그때부터
    # 센다: t=100 은 10초, t=110 은 20초 ≥ grace 15
    for i, expected in [(0, CAUGHT_UP), (1, BACKLOG), (2, BACKLOG)]:
        fakes.local = 20 + i
        fakes.remote = RemoteTxid(18)
        m.refresh_once()
        assert m.report()["status"] == expected, (i, m.report())
        mono[0] += 10.0
