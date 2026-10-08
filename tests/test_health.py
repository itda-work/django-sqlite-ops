"""복제 헬스 (DESIGN §7).

상태 계산은 순수 함수 표 테스트로, 스레드는 가짜 조회 함수로, 뷰는 새 인터프리터의 Django
(``settings.configure()``, ``test_checks.py``·``test_doctor.py`` 와 같은 방식)로 본다.
실제 Litestream 은 ``file://`` 복제본으로 본다(S3 끊김 L8 은 랩 #10 몫).
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _litestream import needs_litestream

from django_sqlite_ops import health
from django_sqlite_ops.boot.decide import RemoteEmpty, RemoteError, RemoteTxid
from django_sqlite_ops.health import (
    BACKLOG,
    CAUGHT_UP,
    UNKNOWN,
    AliasConfig,
    HealthConfig,
    Monitor,
    Sample,
    alias_status,
    next_backlog_since,
    overall_status,
    parse_config,
    read_boot_state,
)

ROOT = Path(__file__).resolve().parent.parent
SQLITE = {"ENGINE": "django.db.backends.sqlite3", "NAME": "/srv/app/app.sqlite3"}

# --- next_backlog_since ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("prev", "local", "remote", "expected"),
    [
        (None, 5, RemoteTxid(5), None),  # 같음
        (10.0, 5, RemoteTxid(5), None),  # 따라잡으면 지운다
        (None, 6, RemoteTxid(5), 100.0),  # 앞서기 시작
        (40.0, 6, RemoteTxid(5), 40.0),  # 이어지면 유지
        (40.0, 4, RemoteTxid(5), None),  # 원격 앞섬
        (40.0, None, RemoteTxid(5), None),  # 메타 없음
        (40.0, 6, RemoteEmpty(), None),
        (40.0, 6, RemoteError("x"), None),
        (40.0, 6, None, None),
    ],
)
def test_next_backlog_since(prev, local, remote, expected):
    assert next_backlog_since(prev, local, remote, 100.0) == expected


# --- alias_status ---------------------------------------------------------------------

BOOT_OK = {"state": "match", "action": "proceed", "unknown_at_boot": False}
BOOT_KEEP = {"state": "unknown", "action": "keep_local", "unknown_at_boot": True}


SAME = RemoteTxid(5)


def sample(local=5, remote=SAME, *, at=1000.0, since=None, boot=None, error=None):
    return Sample(at, "/srv/app/app.sqlite3", local, remote, boot, None, error, since)


@pytest.mark.parametrize(
    ("s", "now", "status", "word"),
    [
        (None, 1000.0, UNKNOWN, "not checked yet"),
        (sample(), 1000.0, CAUGHT_UP, "same TXID"),
        (sample(), 1045.0, CAUGHT_UP, "same TXID"),  # REFRESH×3 = 45 경계는 아직 신선
        (sample(), 1045.1, UNKNOWN, "stuck"),  # 오래된 결과
        (sample(), 999.0, UNKNOWN, "backwards"),
        (sample(error="path is not real"), 1000.0, UNKNOWN, "path is not real"),
        (sample(boot=BOOT_KEEP), 1000.0, UNKNOWN, "unknown_at_boot"),
        (sample(boot=BOOT_OK), 1000.0, CAUGHT_UP, "same TXID"),
        (sample(remote=RemoteError("boom")), 1000.0, UNKNOWN, "remote lookup failed: boom"),
        (sample(remote=RemoteEmpty()), 1000.0, UNKNOWN, "replica is empty"),
        (sample(local=None), 1000.0, UNKNOWN, "local Litestream metadata"),
        (sample(local=4), 1000.0, UNKNOWN, "another machine"),
        # 로컬 앞섬: grace(60) 경계
        (sample(local=6, since=1000.0), 1000.0, CAUGHT_UP, "within BACKLOG_GRACE"),
        (sample(local=6, since=940.1), 1000.0, CAUGHT_UP, "within BACKLOG_GRACE"),
        (sample(local=6, since=940.0), 1000.0, BACKLOG, "ahead of the replica for 60s"),
        (sample(local=6, since=900.0), 1000.0, BACKLOG, "ahead"),
        # 오래된 결과는 backlog 보다 먼저다
        (sample(local=6, since=900.0), 1100.0, UNKNOWN, "stuck"),
    ],
)
def test_alias_status_table(s, now, status, word):
    result = alias_status(s, now, refresh=15, grace=60)
    assert result["status"] == status
    assert word in result["reason"]


def test_alias_status_fields():
    result = alias_status(
        sample(local=0x1A, remote=RemoteTxid(0x19), since=990.0, boot=BOOT_OK),
        1002.5,
        refresh=15,
        grace=60,
    )
    assert result == {
        "status": CAUGHT_UP,
        "reason": result["reason"],
        "path": "/srv/app/app.sqlite3",
        "local_txid": "000000000000001a",
        "remote_txid": "0000000000000019",
        "checked_at": "1970-01-01T00:16:40Z",
        "age": 2.5,
        "backlog_since": "1970-01-01T00:16:30Z",
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
    assert config.databases[0].litestream_config == os.path.abspath("ls.yml")
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
    def __init__(self, db: str, local=5, remote=SAME):
        self.db = db
        self.local = local
        self.remote = remote
        self.calls = 0
        self.fail = False

    def remote_fn(self, cfg, path):
        self.calls += 1
        if self.fail:
            raise RuntimeError("probe exploded")
        return self.remote

    def local_fn(self, path, meta):
        return self.local

    def path_of(self, alias):
        return self.db, None


def monitor(tmp_path, *, refresh=0.05, grace=60.0, clock=time.time, **kw):
    fakes = Fakes(str(tmp_path / "app.sqlite3"), **kw)
    config = HealthConfig((AliasConfig("default", "/etc/litestream.yml"),), refresh, grace)
    m = Monitor(
        config, remote=fakes.remote_fn, local=fakes.local_fn, path_of=fakes.path_of, clock=clock
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


def test_monitor_refreshes_periodically(tmp_path):
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
        assert "s3 down" in report["databases"]["default"]["reason"]
    finally:
        m.stop(5)


def test_monitor_survives_exceptions(tmp_path):
    m, fakes = monitor(tmp_path)
    fakes.fail = True
    m.ensure_started()
    try:
        assert wait_for(lambda: m.refreshes >= 2)
        reason = m.report()["databases"]["default"]["reason"]
        assert "RuntimeError: probe exploded" in reason
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
    assert "OSError: weird" in m.report()["databases"]["default"]["reason"]


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
    m, fakes = monitor(tmp_path, grace=60.0, refresh=15.0, clock=lambda: now[0], local=6)
    m.refresh_once()
    db = m.report()["databases"]["default"]
    assert db["status"] == CAUGHT_UP and db["backlog_since"] == "1970-01-01T00:16:40Z"
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
    m, _ = monitor(tmp_path, refresh=15.0, clock=lambda: now[0])
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
    assert "meta_path is not a real path" in m.report()["databases"]["default"]["reason"]


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

from django.urls import path

from django_sqlite_ops import health
from django_sqlite_ops.boot.decide import RemoteTxid

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
        cfg, remote=lambda c, p: RemoteTxid(fake["remote"]), local=lambda p, m: fake["local"]
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
        "reason",
        "path",
        "local_txid",
        "remote_txid",
        "checked_at",
        "age",
        "backlog_since",
        "boot_state",
        "boot_state_error",
    }
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
    m = Monitor(cfg, path_of=lambda alias: (str(db), None), clock=lambda: now[0])

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
