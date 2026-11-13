"""복제 파이프라인 정지를 ``caught_up`` 이 감추지 않는다 (#115, DESIGN §7).

- (A) 컴팩션 정지: 복제본에서 L1 이 덮지 않은 가장 오래된 L0 가 가장 새 객체보다
  ``COMPACTION_GRACE`` 이상 오래됐으면 ``caught_up`` 대신 ``backlog``(``compaction_stalled``).
  같은 ``litestream ltx -level all -json`` 출력에서 계산한다(추가 조회 없음).
- (B) Litestream 이 L0 를 쓰지 않음: 최신 로컬 L0 가 그대로인 채 DB 가 바뀐 상태를, WAL 근거와
  파일 시각 근거가 번갈아 나와도 이어서 센다(``pending_since`` 의 키를 최신 L0 이름으로 통일).

순수 함수·주입 시계로 판정을 보고, 실제 Litestream(파일 복제본)으로 손상 주입을 본다.
"""

import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from django_sqlite_ops import health
from django_sqlite_ops.boot import litestream
from django_sqlite_ops.boot.decide import RemoteError, RemoteTxid
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
    next_pending_since,
    next_wal_pending_since,
    parse_config,
)
from django_sqlite_ops.wal import IN_SYNC, NO_EVIDENCE, PENDING, WalEvidence

SQLITE = {"ENGINE": "django.db.backends.sqlite3", "NAME": "/srv/app/app.sqlite3"}
L0 = "0000000000000010-0000000000000010.ltx"
L0_NEXT = "0000000000000011-0000000000000011.ltx"


def summary(lag=0.0, *, uncompacted=None, compacted=0x10, problem=None):
    """``newest_at`` − ``oldest_uncompacted_at`` = ``lag`` 인 요약."""
    if problem is not None:
        return litestream.LtxSummary(0, 0, 0, None, None, problem)
    count = (1 if lag else 0) if uncompacted is None else uncompacted
    oldest = 1000.0 if count else None
    return litestream.LtxSummary(count + 3, compacted, count, oldest, 1000.0 + lag)


W_SYNC = WalEvidence(IN_SYNC, "every -wal commit is within the latest L0", 4152, None, True, "k")
W_PEND = WalEvidence(PENDING, "the -wal has commits after the latest L0", 4152, 8272, True, "k")
W_NONE = WalEvidence(NO_EVIDENCE, "no readable -wal file", 4152)
IDLE = FileTimes(10.0, 11.0, (L0, 1))


SAME = RemoteTxid(5)


def sample(*, local=5, remote=SAME, wal=W_SYNC, ltx=None, at=1000.0, **kw):
    path = "/srv/app/app.sqlite3"
    return Sample(at, at, path, local, remote, None, None, files=IDLE, wal=wal, ltx=ltx, **kw)


def verdict(s, *, now=1000.0, grace=60.0, compaction_grace=300.0):
    entry = alias_status(s, now, refresh=15.0, grace=grace, compaction_grace=compaction_grace)
    return entry["status"], entry["code"], entry["reason"]


# --- (A) 판정 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "base_code"),
    [
        ({}, "in_sync"),
        ({"local": 6, "backlog_since": Since(990.0, 990.0, 6)}, "local_ahead_within_grace"),
        (
            {"wal": W_PEND, "pending_since": Since(990.0, 990.0, ("l0", L0))},
            "db_changed_within_grace",
        ),
    ],
)
def test_caught_up_with_stalled_compaction_is_backlog(kw, base_code):
    assert verdict(sample(ltx=summary(10.0), **kw))[:2] == (CAUGHT_UP, base_code)
    status, code, reason = verdict(sample(ltx=summary(412.0, uncompacted=431), **kw))
    assert (status, code) == (BACKLOG, "compaction_stalled")
    assert "412s" in reason and "COMPACTION_GRACE 300s" in reason
    assert "431 L0" in reason and "0000000000000010" in reason
    assert "compaction failed" in reason


@pytest.mark.parametrize(
    ("lag", "status"), [(299.0, CAUGHT_UP), (300.0, BACKLOG), (301.0, BACKLOG)]
)
def test_compaction_grace_boundary(lag, status):
    assert verdict(sample(ltx=summary(lag)))[0] == status


def test_compaction_does_not_override_other_states():
    """다른 판정(unknown·backlog)은 그대로다. 바뀌는 것은 caught_up 뿐이다."""
    stalled = summary(1000.0)
    assert verdict(sample(remote=RemoteError("x"), ltx=stalled))[:2] == (UNKNOWN, "remote_error")
    assert verdict(sample(local=None, ltx=stalled))[:2] == (UNKNOWN, "no_local_meta")
    behind = sample(local=6, backlog_since=Since(900.0, 900.0, 6), ltx=stalled)
    assert verdict(behind)[:2] == (BACKLOG, "local_ahead")
    pending = sample(wal=W_PEND, pending_since=Since(900.0, 900.0, ("l0", L0)), ltx=stalled)
    assert verdict(pending)[:2] == (BACKLOG, "db_not_replicated")
    assert verdict(sample(wal=W_NONE, ltx=stalled))[:2] == (UNKNOWN, "no_wal_evidence")


def test_compaction_check_can_be_disabled():
    assert verdict(sample(ltx=summary(10_000.0)), compaction_grace=None)[:2] == (
        CAUGHT_UP,
        "in_sync",
    )


def test_unreadable_listing_is_not_caught_up():
    """같은 조회 결과에서 컴팩션을 판정할 수 없으면 caught_up 이라 하지 않는다."""
    bad = summary(problem="unexpected litestream ltx output: item 3 has no readable timestamp")
    status, code, reason = verdict(sample(ltx=bad))
    assert (status, code) == (UNKNOWN, "no_compaction_evidence")
    assert "timestamp" in reason
    # 끄면 보지 않는다
    assert verdict(sample(ltx=bad), compaction_grace=None)[:2] == (CAUGHT_UP, "in_sync")


def test_probe_without_listing_keeps_previous_behaviour():
    """요약을 주지 않는 조회(``Remote`` 만 돌려주는 주입 함수)는 지금처럼 판정한다."""
    assert verdict(sample(ltx=None))[:2] == (CAUGHT_UP, "in_sync")


# --- 설정 -------------------------------------------------------------------------------


def test_compaction_grace_config():
    raw = {"DATABASES": {"default": {"litestream_config": "/etc/ls.yml"}}}
    cfg, errors = parse_config(raw, {"default": SQLITE})
    assert errors == [] and cfg.compaction_grace == health.DEFAULT_COMPACTION_GRACE == 300.0
    for value, expected in [(60, 60.0), (0.5, 0.5), (None, None)]:
        cfg, errors = parse_config({**raw, "COMPACTION_GRACE": value}, {"default": SQLITE})
        assert errors == [] and cfg.compaction_grace == expected, value
    for value in (0, -1, "300", True, float("inf"), float("nan")):
        cfg, errors = parse_config({**raw, "COMPACTION_GRACE": value}, {"default": SQLITE})
        assert cfg is None and "COMPACTION_GRACE" in errors[0][0], value


def test_response_schema_is_unchanged():
    """응답 필드를 더하지 않는다(#40 과 같은 원칙). 숫자는 reason 에 넣는다."""
    entry = alias_status(sample(ltx=summary(1000.0)), 1000.0, refresh=15.0, grace=60.0)
    plain = alias_status(sample(), 1000.0, refresh=15.0, grace=60.0)
    assert set(entry) == set(plain)
    assert health.SCHEMA_VERSION == 1


# --- 갱신 스레드에 요약이 들어가는가 -----------------------------------------------------


def _monitor(tmp_path, remote, *, compaction_grace=300.0, wal=W_SYNC, files=IDLE, mono=None):
    db = tmp_path / "app.sqlite3"
    db.write_bytes(b"x" * 4096)
    config = HealthConfig(
        (AliasConfig("default", "/etc/ls.yml"),), 15.0, 60.0, compaction_grace=compaction_grace
    )
    kw = {"monotonic": (lambda: mono[0])} if mono is not None else {}
    return Monitor(
        config,
        remote=remote,
        local=lambda p, m: 5,
        files=(files if callable(files) else (lambda p, m: files)),
        wal=(wal if callable(wal) else (lambda p, m: wal)),
        path_of=lambda alias: (str(db), None),
        **kw,
    )


def test_monitor_uses_listing_summary(tmp_path):
    m = _monitor(tmp_path, lambda c, p: (RemoteTxid(5), summary(400.0)))
    m.refresh_once()
    assert m.report()["databases"]["default"]["code"] == "compaction_stalled"
    assert m.report()["status"] == BACKLOG
    m = _monitor(tmp_path, lambda c, p: (RemoteTxid(5), summary(400.0)), compaction_grace=None)
    m.refresh_once()
    assert m.report()["databases"]["default"]["code"] == "in_sync"


def test_default_probe_is_the_listing(monkeypatch, tmp_path):
    seen = {}

    def fake_listing(db, *, config, binary):
        seen["args"] = (db, config, binary)
        return RemoteTxid(5), summary(400.0)

    monkeypatch.setattr(litestream, "remote_listing", fake_listing)
    db = tmp_path / "app.sqlite3"
    db.write_bytes(b"x" * 4096)
    config = HealthConfig((AliasConfig("default", "/etc/ls.yml"),), 15.0, 60.0)
    m = Monitor(
        config,
        local=lambda p, m: 5,
        files=lambda p, m: IDLE,
        wal=lambda p, m: W_SYNC,
        path_of=lambda alias: (str(db), None),
    )
    m.refresh_once()
    assert seen["args"][1] == "/etc/ls.yml"
    assert m.report()["databases"]["default"]["code"] == "compaction_stalled"


# --- (B) pending 키 통일 ----------------------------------------------------------------


def test_pending_keys_name_the_latest_l0():
    wal = WalEvidence(PENDING, "", 4152, 8272, True, (L0, 8272, 1, 2))
    files = FileTimes(20.0, 10.0, (L0, 10_000_000_000))
    a = next_wal_pending_since(None, wal, 100.0, 7.0)
    b = next_pending_since(None, files, 100.0, 7.0)
    assert a.key == b.key == ("l0", L0)
    assert next_pending_since(a, files, 200.0, 8.0) is a  # 근거가 바뀌어도 같은 L0: 이어 센다
    assert next_wal_pending_since(b, wal, 200.0, 8.0) is b
    moved = FileTimes(20.0, 15.0, (L0_NEXT, 15_000_000_000))
    assert next_pending_since(a, moved, 200.0, 8.0).key == ("l0", L0_NEXT)  # L0 가 바뀌면 다시


def test_alternating_evidence_on_a_frozen_l0_is_backlog(tmp_path):
    """#106 Q3 dead: CONN_MAX_AGE=0 앱 + Litestream 없음이면 마지막 연결 닫기마다 -wal 이
    지워졌다 생겨 WAL 근거(PENDING)와 파일 시각 근거가 번갈아 나온다. 최신 L0 가 그대로이면
    grace 뒤 backlog 다(수정 전에는 매번 다시 세어 caught_up·unknown 을 오갔다)."""
    restarted = WalEvidence(
        PENDING,
        "the -wal restarted after the latest L0 and has commits not in it",
        4152,
        8272,
        False,
        (L0, 4152, 1, 2),
    )
    newer = FileTimes(2000.0, 1000.0, (L0, 1000_000_000_000), 2000.0, None)
    turn = [0]
    mono = [0.0]

    def wal(p, m):
        return restarted if turn[0] % 2 == 0 else W_NONE

    m = _monitor(tmp_path, lambda c, p: RemoteTxid(5), wal=wal, files=newer, mono=mono)
    seen = []
    for _ in range(12):
        m.refresh_once()
        e = m.report()["databases"]["default"]
        seen.append((mono[0], e["status"], e["code"]))
        turn[0] += 1
        mono[0] += 10.0
    # grace 60초: t=60 부터 backlog
    assert all(s == CAUGHT_UP or c == "no_wal_evidence" for t, s, c in seen if t < 60), seen
    assert all((s, c) == (BACKLOG, "db_not_replicated") for t, s, c in seen if t >= 60), seen


def test_in_sync_still_clears_pending(tmp_path):
    turn = [0]
    mono = [0.0]
    restarted = WalEvidence(PENDING, "", 4152, 8272, False, (L0, 4152, 1, 2))
    newer = FileTimes(2000.0, 1000.0, (L0, 1000_000_000_000))
    order = [restarted, W_NONE, W_SYNC, W_NONE, restarted]

    def wal(p, m):
        return order[turn[0]]

    m = _monitor(tmp_path, lambda c, p: RemoteTxid(5), wal=wal, files=newer, mono=mono)
    starts = []
    for _ in order:
        m.refresh_once()
        ps = m.samples["default"].pending_since
        starts.append(None if ps is None else ps.mono)
        turn[0] += 1
        mono[0] += 10.0
    # IN_SYNC(t=20)이 지운다. 그 뒤 파일 시각 근거(t=30)부터 다시 센다
    assert starts == [0.0, 0.0, None, 30.0, 30.0], starts


# --- 실제 Litestream (파일 복제본) --------------------------------------------------------
#
# 짧은 levels(L1 4초)·보존으로 시간을 줄인다. 로컬 메타의 L0 를 손상시키는 방식은 #106 Q4 와
# 기획 §1-3 실험과 같다(헤더 뒤 64바이트 뒤집기).

L1_INTERVAL = 4.0


def _ls_config(lab: Path, db: Path) -> Path:
    path = lab / "litestream.yml"
    path.write_text(
        "l0-retention: 10s\n"
        "l0-retention-check-interval: 2s\n"
        "levels:\n"
        f"  - interval: {L1_INTERVAL:g}s\n"
        f"dbs:\n  - path: {db}\n    replica:\n      type: file\n      path: {lab / 'replica'}\n"
    )
    return path


class Writer:
    """``interval`` 초마다 행 하나를 쓴다. ``hold`` 면 연결을 쥐고, 아니면 매번 열고 닫는다."""

    def __init__(self, db: Path, *, hold: bool, interval: float = 0.2):
        self.db, self.hold, self.interval = db, hold, interval
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.errors: list[BaseException] = []
        self.rows = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _connect(self):
        con = sqlite3.connect(self.db, isolation_level=None, timeout=10)
        con.execute("pragma busy_timeout=10000")
        return con

    def _run(self):
        con = self._connect() if self.hold else None
        try:
            while not self.stop.is_set():
                if not self.paused.is_set():
                    c = con or self._connect()
                    try:
                        c.execute("insert into t(v) values (randomblob(500))")
                        self.rows += 1
                    finally:
                        if con is None:
                            c.close()
                self.stop.wait(self.interval)
        except BaseException as exc:  # noqa: BLE001
            self.errors.append(exc)
        finally:
            if con is not None:
                con.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(15)
        assert not self.errors, self.errors


class Reader:
    """요청이 겹치는 앱을 흉내 낸다: 연결 하나를 ``hold`` 초 쥐었다 닫고 ``rest`` 초 쉰다.

    쥔 동안은 쓰기 쪽의 연결 닫기가 '마지막 연결'이 아니어서 ``-wal`` 이 남고(L0 뒤 커밋 →
    WAL 근거), 닫으면 마지막 연결 닫기의 체크포인트가 ``-wal`` 을 지운다(근거 없음 → 파일 시각).
    #106 Q3 dead 처럼 두 근거가 번갈아 나오게 한다.
    """

    def __init__(self, db: Path, *, hold: float = 1.5, rest: float = 1.5):
        self.db, self.hold, self.rest = db, hold, rest
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            while not self.stop.is_set():
                con = sqlite3.connect(self.db, timeout=10)
                try:
                    con.execute("select count(*) from t").fetchone()
                    self.stop.wait(self.hold)
                finally:
                    con.close()
                self.stop.wait(self.rest)
        except BaseException as exc:  # noqa: BLE001
            self.errors.append(exc)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(15)
        assert not self.errors, self.errors


def _setup(tmp_path, binary, *, grace=4.0, compaction_grace=8.0):
    lab = tmp_path / "lab"
    lab.mkdir()
    db = lab / "app.sqlite3"
    con = sqlite3.connect(db, isolation_level=None)
    con.execute("pragma journal_mode=wal")
    con.execute("create table t(id integer primary key, v blob)")
    con.close()
    config = _ls_config(lab, db)
    cfg = HealthConfig(
        (AliasConfig("default", str(config), None, binary),),
        1.0,
        grace,
        compaction_grace=compaction_grace,
    )
    m = Monitor(cfg, path_of=lambda alias: (str(db), None))
    return lab, db, config, m


def _replicate(binary, config, lab):
    log = open(lab / "litestream.log", "w")  # noqa: SIM115 - 프로세스와 함께 닫는다
    proc = subprocess.Popen(
        [binary, "replicate", "-config", str(config)], stdout=log, stderr=subprocess.STDOUT
    )
    return proc, log


def _stop(proc, log):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()  # 손상 뒤 백오프 중이면 SIGTERM 에 늦게 끝난다(기획 §1-3 실험 1)
        proc.wait(timeout=15)
    log.close()


def _entry(m):
    m.refresh_once()
    return m.report()["databases"]["default"]


def _until(m, want, timeout):
    """``want(entry)`` 가 참이 될 때까지 1초마다 갱신한다. 본 (status, code) 를 돌려준다."""
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        e = _entry(m)
        seen.append((e["status"], e["code"]))
        if want(e):
            return e, seen
        time.sleep(1.0)
    pytest.fail(f"timed out; seen {seen[-10:]}")


def _l0_dir(db: Path) -> Path:
    return db.parent / f".{db.name}-litestream" / "ltx" / "0"


def _corrupt(path: Path) -> None:
    with open(path, "r+b") as f:
        f.seek(120)
        data = f.read(64)
        f.seek(120)
        f.write(bytes(b ^ 0xFF for b in data))


def _l0_files(db: Path) -> list[Path]:
    return sorted(p for p in _l0_dir(db).iterdir() if p.name.endswith(".ltx"))


def _l0_mtimes(db: Path) -> list[tuple[Path, float]]:
    """로컬 L0 와 mtime. 보존 정리가 도중에 지운 파일은 뺀다."""
    found = []
    for p in _l0_files(db):
        try:
            found.append((p, p.stat().st_mtime))
        except FileNotFoundError:
            pass
    return found


def _caught_up_for(m, seconds):
    end = time.monotonic() + seconds
    seen = []
    while time.monotonic() < end:
        e = _entry(m)
        seen.append((e["status"], e["code"]))
        time.sleep(1.0)
    return seen


def test_real_corrupt_uncompacted_l0_is_compaction_stalled(tmp_path, litestream_binary):
    """최신이 아닌 미컴팩션 로컬 L0 하나가 깨지면 L1 컴팩션만 멈춘다(L0 업로드는 계속,
    TXID 같음). 그동안 caught_up 이던 헬스가 COMPACTION_GRACE 뒤 backlog/compaction_stalled."""
    lab, db, config, m = _setup(tmp_path, litestream_binary)
    proc, log = _replicate(litestream_binary, config, lab)
    try:
        with Writer(db, hold=True):
            _until(m, lambda e: e["status"] == CAUGHT_UP, 30)
            normal = _caught_up_for(m, 6)
            assert all(s == CAUGHT_UP for s, _ in normal), normal
            # 다음 L1 경계(벽시계 L1_INTERVAL 배수)까지 0.5초 넘게 남은 때, 마지막 경계 0.5초
            # 뒤에 생긴(아직 컴팩션되지 않은) L0 중 최신이 아닌 것을 깨뜨린다. 최신 L0 를 깨뜨리면
            # sync 자체가 멈춘다(아래 테스트).
            deadline = time.monotonic() + 30
            while True:
                assert time.monotonic() < deadline, "no uncompacted L0 to corrupt"
                now = time.time()
                boundary = now - (now % L1_INTERVAL)
                fresh = [p for p, mtime in _l0_mtimes(db) if mtime > boundary + 0.5]
                if now % L1_INTERVAL < L1_INTERVAL - 0.5 and len(fresh) >= 2:
                    _corrupt(fresh[-2])
                    corrupted_at = time.monotonic()
                    break
                time.sleep(0.05)
            e, seen = _until(m, lambda e: e["status"] != CAUGHT_UP, 60)
            assert (e["status"], e["code"]) == (BACKLOG, "compaction_stalled"), seen
            assert "COMPACTION_GRACE 8s" in e["reason"]
            # 지연은 정지 뒤 벽시계로 늘므로 문턱 + L1 주기 + 갱신·초 단위 시각의 여유 안에 온다
            assert time.monotonic() - corrupted_at < 8 + L1_INTERVAL + 10, seen
            # L0 는 계속 올라가므로 TXID 판정은 caught_up 이었다
            assert e["local_txid"] == e["remote_txid"] or e["local_txid"] > e["remote_txid"]
    finally:
        _stop(proc, log)
    assert "compaction failed" in (lab / "litestream.log").read_text()


# 최신 로컬 L0 손상은 판마다 다르다(#115, 재현함 — 각 판 3회).
# - 0.5.17: 다음 sync 의 lastPageMatch() 가 디스크의 최신 L0 페이지를 풀다가 실패해 sync 가
#   멈춘다(`sync error … last page match: decode ltx page`, 백오프). L0 기록이 멈추므로 컴팩션
#   지연은 늘지 않고, 연결을 쥔 앱이 계속 쓰면 -wal 에 최신 L0 뒤 커밋이 남아 BACKLOG_GRACE 뒤
#   db_not_replicated 다.
# - 0.5.18: 마지막으로 쓴 L0 의 마지막 페이지를 메모리에 두고(db.go `lastLTXPage`, 82·2369–2371행)
#   lastPageMatch() 가 그것과 비교하므로(1946행) 디스크의 손상을 읽지 않는다. sync error 0줄,
#   L0 기록은 계속되고 L1 컴팩션만 그 L0 를 읽다 실패한다 → compaction_stalled.
# 표에 없는 판은 기대가 없으므로 실패한다(통과시키지 않는다).
LATEST_L0_CORRUPTION = {
    "0.5.17": {"code": "db_not_replicated", "sync_error": True},
    "0.5.18": {"code": "compaction_stalled", "sync_error": False},
}


def test_real_corrupt_latest_l0(tmp_path, litestream_binary):
    """최신 로컬 L0 손상: 두 판 모두 caught_up 이 아니게 되지만 사유는 판별이다(위 표)."""
    version = litestream.version(binary=litestream_binary)
    want = LATEST_L0_CORRUPTION.get(version or "")
    assert want is not None, f"no expectation for litestream {version}"
    lab, db, config, m = _setup(tmp_path, litestream_binary)
    proc, log = _replicate(litestream_binary, config, lab)
    try:
        with Writer(db, hold=True) as w:
            _until(m, lambda e: e["status"] == CAUGHT_UP, 30)
            # 쓰기를 멈추고 Litestream 이 마지막 커밋을 L0 로 쓸 때까지 기다린다(그 L0 가 최신).
            # 그 L0 가 이미 L1 로 컴팩션됐으면 0.5.18 은 디스크의 L0 를 다시 읽지 않아 손상이
            # 드러나지 않는다(전체 실행에서 1회 재현함). 그래서 마지막 L1 경계 0.5초 뒤에 생겼고
            # 다음 경계까지 0.5초 넘게 남았을 때만 깨뜨리고, 아니면 쓰기를 잠깐 재개해 다시 본다.
            deadline = time.monotonic() + 60
            tries = []
            while True:
                assert time.monotonic() < deadline, f"no uncompacted latest L0 to corrupt: {tries}"
                w.paused.set()
                _until(m, lambda e: e["code"] == "in_sync", 30)
                now = time.time()
                boundary = now - (now % L1_INTERVAL)
                latest, mtime = _l0_mtimes(db)[-1]
                tries.append((round(mtime - boundary, 2), round(now % L1_INTERVAL, 2)))
                if mtime > boundary + 0.5 and now % L1_INTERVAL < L1_INTERVAL - 0.5:
                    _corrupt(latest)
                    break
                # 다음 경계 1초 뒤까지 쓴다. 0.3초만 넘기면 Litestream 의 1초 tick 위상이 경계
                # 바로 뒤(0.5초 안)일 때 마지막 L0 가 매번 그 tick 에 생겨 창을 영영 못 찾았다(1회).
                w.paused.clear()
                time.sleep(1.0 + (L1_INTERVAL - now % L1_INTERVAL))
            w.paused.clear()
            e, seen = _until(m, lambda e: e["status"] != CAUGHT_UP, 60)
            assert (e["status"], e["code"]) == (BACKLOG, want["code"]), seen
    finally:
        _stop(proc, log)
    text = (lab / "litestream.log").read_text()
    assert ('msg="sync error"' in text) is want["sync_error"], text[-2000:]
    assert "compaction failed" in text or want["sync_error"], text[-2000:]


def test_real_no_litestream_with_closing_connections_is_backlog(tmp_path, litestream_binary):
    """#106 Q3 dead: 요청마다 연결을 닫는 앱(CONN_MAX_AGE=0)에서 Litestream 이 없으면 -wal 이
    지워졌다 생겨 근거가 번갈아 나온다. 최신 L0 가 그대로이므로 BACKLOG_GRACE 뒤 backlog."""
    lab, db, config, m = _setup(tmp_path, litestream_binary)
    proc, log = _replicate(litestream_binary, config, lab)
    try:
        with Writer(db, hold=False):
            _until(m, lambda e: e["status"] == CAUGHT_UP, 30)
    finally:
        _stop(proc, log)
    with Writer(db, hold=False, interval=0.3), Reader(db):
        e, seen = _until(m, lambda e: e["status"] == BACKLOG, 30)
    assert e["code"] == "db_not_replicated", seen
    # 두 근거가 실제로 번갈아 나왔는가(수정 전에는 그때마다 다시 세어 backlog 가 되지 않았다)
    codes = {c for _, c in seen}
    assert {"db_changed_within_grace", "no_wal_evidence"} <= codes or len(seen) <= 6, seen
    assert os.path.exists(db)
