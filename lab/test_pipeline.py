"""L9a·L9b: 멈춘 복제 파이프라인을 헬스 ``caught_up`` 이 감추지 않는다 (#115, DESIGN §7·§10).

- **L9a 컴팩션 정지**: 프로필 그대로(``labapp``, ``replicate -exec``), 쓰기 1회/초. 로컬 메타의
  최신이 아닌 미컴팩션 L0 하나를 깨뜨리면 L1 컴팩션만 멈추고 L0 업로드는 계속된다(#106 Q4).
  ``COMPACTION_GRACE`` 60초로 줄여 그 안(+ L1 주기 30초 + 여유)에 ``backlog/compaction_stalled``.
- **L9b Litestream 없음**: 감독 변형(``labapp-sup``), ``CONN_MAX_AGE=0``(프로필 기본), 겹치는 요청
  (``walcp_load``). Litestream 만 SIGTERM 하면 마지막 연결 닫기마다 ``-wal`` 이 지워졌다 생겨 WAL
  근거와 파일 시각 근거가 번갈아 나온다(#106 Q3 dead). ``BACKLOG_GRACE`` 10초 + 여유 안에
  ``backlog/db_not_replicated``, 재기동 뒤 ``caught_up``.

두 판(0.5.17·0.5.18)에서 기대가 같다. 판정은 ``_checks.stall_problems``(Docker 없이
``tests/test_lab.py`` 가 검사).
"""

from __future__ import annotations

import json
import threading
import time

import _checks
from _lab import Stack, http_json, log
from test_scenarios import Volumes, env
from test_walcp import finish_load, lsctl, start_load

REFRESH = 2.0
GRACE = 10.0
COMPACTION_GRACE = 60.0
L1_INTERVAL = 30.0  # 프로필 litestream.yml 은 levels 를 정하지 않는다(Litestream 기본 L1 30초)

# 컨테이너 안에서 돈다. 다음 L1 경계(벽시계 30초 배수)까지 5초 넘게 남고 마지막 경계 2초 뒤에 생긴
# 로컬 L0 가 둘 이상일 때, 그중 최신이 아닌 것의 가운데 64바이트를 뒤집는다. 최신 L0 를 깨뜨리면
# 판에 따라 sync 가 멈추므로(0.5.17, tests/test_health_pipeline.py 의 판별 표) 피한다.
CORRUPT_UNCOMPACTED = """
import json, os, time
meta = "/data/.app.sqlite3-litestream"
interval = %(interval)r
deadline = time.time() + 120
def l0_dir():
    for root, dirs, _ in os.walk(meta):
        parent = os.path.basename(os.path.dirname(root))
        if os.path.basename(root) in ("0", "0000") and parent == "ltx":
            return root
    return None
while time.time() < deadline:
    d = l0_dir()
    now = time.time()
    boundary = now - now %% interval
    if d and now %% interval < interval - 5:
        fresh = []
        for name in sorted(os.listdir(d)):
            if not name.endswith(".ltx"):
                continue
            try:
                mtime = os.stat(os.path.join(d, name)).st_mtime
            except FileNotFoundError:
                continue
            if mtime > boundary + 2:
                fresh.append(name)
        if len(fresh) >= 2:
            path = os.path.join(d, fresh[-2])
            body = bytearray(open(path, "rb").read())
            start = 100 + (len(body) - 100) // 2
            for i in range(start, min(start + 64, len(body))):
                body[i] ^= 0xFF
            open(path, "r+b").write(bytes(body))
            print(json.dumps({"corrupted": path, "size": len(body), "fresh": len(fresh),
                              "into_interval": round(now %% interval, 1)}))
            raise SystemExit
    time.sleep(0.2)
print(json.dumps({"corrupted": None}))
"""


def _health(port: int) -> tuple[str, str]:
    try:
        d = http_json("GET", port, "/internal/sqlite-health", timeout=10)["databases"]["default"]
    except OSError as exc:
        return f"error {type(exc).__name__}", ""
    return d["status"], d["code"]


def _series(port: int, seconds: float, *, stop_at=None, t0: float | None = None) -> list:
    """``REFRESH`` 마다 헬스를 표본한다. ``stop_at((status, code))`` 가 참이면 멈춘다."""
    t0 = time.monotonic() if t0 is None else t0
    out = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        status, code = _health(port)
        out.append((round(time.monotonic() - t0, 1), status, code))
        if stop_at is not None and stop_at((status, code)):
            break
        time.sleep(REFRESH)
    return out


def _wait_caught_up(port: int, timeout: float = 180) -> float:
    t0 = time.monotonic()
    seen = _series(port, timeout, stop_at=lambda h: h[0] == "caught_up")
    assert seen and seen[-1][1] == "caught_up", seen[-10:]
    return round(time.monotonic() - t0, 1)


def _health_env() -> dict[str, str]:
    return {"LAB_HEALTH_REFRESH": f"{REFRESH:g}", "LAB_HEALTH_GRACE": f"{GRACE:g}"}


def test_L9a_compaction_stall_is_backlog(stack: Stack, run_id, rec, litestream_version):
    """미컴팩션 로컬 L0 손상 → 컴팩션만 멈춤 → COMPACTION_GRACE 뒤 backlog/compaction_stalled."""
    prefix = f"l9a-{run_id}"
    e = env(
        Volumes.take(),
        prefix,
        "--init-new",
        **_health_env(),
        LAB_HEALTH_COMPACTION_GRACE=f"{COMPACTION_GRACE:g}",
    )
    port = stack.up_app("labapp", env=e)
    stop = threading.Event()
    errors: list[str] = []

    def writer() -> None:
        while not stop.is_set():
            try:
                http_json("POST", port, "/lab/write?n=5", timeout=30)
            except OSError as exc:
                errors.append(type(exc).__name__)
            stop.wait(1.0)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        rec["caught_up_after_s"] = _wait_caught_up(port)
        # 정상 구간: L1 경계를 한 번 넘도록 L1 주기보다 길게
        before = _series(port, L1_INTERVAL + 10)
        script = CORRUPT_UNCOMPACTED % {"interval": L1_INTERVAL}
        res = stack.compose("exec", "-T", "labapp", "python", "-c", script, timeout=180)
        corrupt = json.loads(res.out.strip().splitlines()[-1])
        rec["corrupt"] = corrupt
        assert corrupt["corrupted"], corrupt
        t0 = time.monotonic()
        deadline = COMPACTION_GRACE + L1_INTERVAL + 40
        want = ("backlog", "compaction_stalled")
        after = _series(port, deadline + 20, stop_at=lambda h: h == want, t0=t0)
    finally:
        stop.set()
        thread.join(30)
    rc, _, logs = stack.stop_app("labapp")
    rec.update(
        before=before,
        after=after,
        write_errors=len(errors),
        compaction_failed_lines=logs.count('msg="compaction failed"'),
        sync_error_lines=logs.count('msg="sync error"'),
        litestream_version=litestream_version,
    )
    log(f"L9a {litestream_version}: after={after[-3:]} corrupt={corrupt}")
    problems = _checks.stall_problems(before, after, want, deadline)
    assert not problems, problems
    assert rec["compaction_failed_lines"] > 0, logs[-3000:]
    assert rec["sync_error_lines"] == 0, logs[-3000:]
    assert not errors, errors


def test_L9b_no_litestream_is_backlog(stack: Stack, run_id, rec, litestream_version):
    """감독 변형에서 Litestream 만 SIGTERM, 요청마다 연결을 닫는 앱이 계속 쓴다 → BACKLOG_GRACE
    뒤 backlog/db_not_replicated. 재기동 뒤 caught_up."""
    service = "labapp-sup"
    prefix = f"l9b-{run_id}"
    e = env(Volumes.take(), prefix, "--init-new", **_health_env())
    port = stack.up_app(service, env=e)
    cma = http_json("GET", port, "/lab/stats")["conn_max_age"]
    assert cma == 0, cma
    lead, stalled, tail = 30.0, 60.0, 60.0
    proc = start_load(stack, e, service, lead + stalled + tail)
    try:
        time.sleep(5)
        rec["caught_up_after_s"] = _wait_caught_up(port)
        before = _series(port, 10)
        rec["ls_stop"] = lsctl(stack, service, "stop", "TERM")
        t0 = time.monotonic()
        deadline = GRACE + 30
        want = ("backlog", "db_not_replicated")
        after = _series(port, stalled, t0=t0)
        rec["ls_start"] = lsctl(stack, service, "start")
        rec["recovered_after_s"] = _wait_caught_up(port, timeout=180)
    finally:
        load = finish_load(proc, lead + stalled + tail)
    rc, _, logs = stack.stop_app(service)
    codes = sorted({c for _, s, c in after})
    rec.update(
        before=before,
        after=after,
        codes_during_stop=codes,
        load_errors=load["errors"],
        litestream_version=litestream_version,
    )
    log(f"L9b {litestream_version}: codes={codes} recovered={rec['recovered_after_s']}s")
    problems = _checks.stall_problems(before, after, want, deadline)
    assert not problems, problems
    # 한 번 backlog 가 되면 Litestream 이 없는 동안 그대로다(근거가 번갈아도 다시 세지 않는다)
    hit = next(t for t, s, c in after if (s, c) == want)
    late = [(t, s, c) for t, s, c in after if t > hit and (s, c) != want]
    assert not late, late
    assert not load["errors"], load["errors"]
