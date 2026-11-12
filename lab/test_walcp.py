"""#106 앱 쪽 ``wal_autocheckpoint`` 와 Litestream, WAL·원격 L0 증가 측정. ``scripts/lab.sh walcp``.

Litestream 판은 ``LAB_LITESTREAM_VERSION`` 으로 고르고, 결과에는 이미지 안의 실제 판을 적는다.
``wal_autocheckpoint`` 는 ``1000``(프로필 그대로 — 설정을 건드리지 않고 연결의 값을 확인한다)과
``0``(``LAB_PRAGMAS`` → ``sqlite_database(pragmas=...)`` → ``init_command``)이다. 실제 연결의 값을
``/lab/stats`` 로 읽어 다르면 실패한다.

- ``test_walcp_replica`` (Q1·Q2): 조건 (ckpt × 방식) 마다 ``LAB_WALCP_REPS`` 번.
  - ``steady``: 프로필 그대로의 ``labapp``(``replicate -exec``). 부하 → 헬스 ``caught_up`` → 복원본
    대조 → ``docker stop`` 뒤 한 번 더 대조.
  - ``restart``(``restart-persist`` 는 + ``CONN_MAX_AGE=None``): 감독 변형 ``labapp-sup``.
    부하 중 ``LAB_WALCP_KILL_AT`` 초에 Litestream 만 SIGTERM,
    ``LAB_WALCP_HOLD`` 초 뒤 다시 띄운다(앱은 계속 쓴다). 그 뒤는 같다.
- ``test_walcp_plain`` (Q2 대조): Litestream 없는 ``labapp-plain`` 에 같은 부하, ckpt 마다 한 번.
- ``test_walcp_stall`` (Q3): ckpt × {``hang``: 프로필 그대로에서 Litestream(PID 1)에 SIGSTOP — 읽기
  잠금을 쥔 채 멈춤, ``dead``: 감독 변형에서 Litestream 을 SIGTERM — 프로세스 없음,
  ``dead-persist``: ``dead`` + ``CONN_MAX_AGE=None``}. 정지
  ``LAB_WALCP_STALL`` 초 동안 WAL 증가율·지연, 재개 뒤 ``caught_up`` 까지 시간과 복원본 대조.
- ``test_walcp_l0`` (Q4): 원격 L0 객체 하나를 깨뜨린 뒤 ``LAB_WALCP_L0_MINUTES`` 분 동안 레벨별 객체
  수·헬스·boot 와 같은 원격 조회·Litestream 오류 줄을 표본한다.

결과: ``lab/.out/walcp-<run>-<test>.json``. 표는 ``lab/walcp_report.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

import _walcpstat as st
import pytest
from _lab import OUT, RUN_ID, Stack, http_json, log
from test_scenarios import Volumes, env

REPS = int(os.environ.get("LAB_WALCP_REPS", "3"))
CKPTS = tuple(int(c) for c in os.environ.get("LAB_WALCP_CKPT", "1000,0").split(","))
MODES = tuple(os.environ.get("LAB_WALCP_MODES", "steady,restart").split(","))
DURATION = float(os.environ.get("LAB_WALCP_DURATION", "90"))
KILL_AT = float(os.environ.get("LAB_WALCP_KILL_AT", "30"))
HOLD = float(os.environ.get("LAB_WALCP_HOLD", "20"))
STALL = float(os.environ.get("LAB_WALCP_STALL", "180"))
STALL_KINDS = tuple(os.environ.get("LAB_WALCP_STALL_KINDS", "hang,dead,dead-persist").split(","))
L0_MINUTES = float(os.environ.get("LAB_WALCP_L0_MINUTES", "10"))
# Q4 손상 대상: remote(복제본의 L0 객체) / local(로컬 메타의 L0 파일)
L0_TARGET = os.environ.get("LAB_WALCP_L0_TARGET", "remote")
LOCAL_CORRUPT = """
import json, os
meta = "/data/.app.sqlite3-litestream"
found = []
for root, _, files in os.walk(meta):
    for f in files:
        if f.endswith(".ltx") and os.path.basename(root) in ("0", "0000"):
            found.append(os.path.join(root, f))
found.sort(key=os.path.basename)
tree = sorted(os.path.relpath(os.path.join(r, d), meta) for r, ds, _ in os.walk(meta) for d in ds)
if not found:
    print(json.dumps({"corrupted": [], "tree": tree}))
    raise SystemExit
path = found[-1]
body = bytearray(open(path, "rb").read())
start = 100 + (len(body) - 100) // 2
for i in range(start, min(start + 64, len(body))):
    body[i] ^= 0xFF
open(path, "r+b").write(bytes(body))
out = {"corrupted": [{"path": path, "size": len(body)}], "local_l0": len(found)}
print(json.dumps({**out, "tree": tree}))
"""
LOAD = {
    "writers": int(os.environ.get("LAB_WALCP_WRITERS", "2")),
    "readers": int(os.environ.get("LAB_WALCP_READERS", "2")),
    "rows": int(os.environ.get("LAB_WALCP_ROWS", "10")),
    "size": int(os.environ.get("LAB_WALCP_SIZE", "3500")),
    "rate": float(os.environ.get("LAB_WALCP_RATE", "30")),
}
INTERVAL = 5.0
HEALTH_ENV = {"LAB_HEALTH_REFRESH": "2", "LAB_HEALTH_GRACE": "10"}
LOG_NEEDLES = re.compile(r"checkpoint|snapshot|reset|salt|truncat|compact|level=(WARN|ERROR)", re.I)


def pragmas_env(ckpt: int) -> dict[str, str]:
    """1000 은 프로필 그대로(설정을 더하지 않는다), 그 밖은 PRAGMA 를 더한다."""
    return {} if ckpt == 1000 else {"LAB_PRAGMAS": json.dumps({"wal_autocheckpoint": ckpt})}


def check_ckpt(port: int, ckpt: int) -> dict:
    stats = http_json("GET", port, "/lab/stats")
    got = stats["pragmas"]["wal_autocheckpoint"]
    assert got == ckpt, f"wal_autocheckpoint on the live connection is {got}, want {ckpt}"
    assert stats["pragmas"]["journal_mode"] == "wal", stats
    return stats["pragmas"]


def start_load(stack: Stack, e: dict, service: str, duration: float) -> subprocess.Popen:
    argv = [
        *stack.argv(),
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "loadgen",
        "/app/lab_tools/walcp_load.py",
        "--url",
        f"http://{service}:8000",
        "--duration",
        str(duration),
        "--interval",
        str(INTERVAL),
        *[x for k, v in LOAD.items() for x in (f"--{k}", str(v))],
    ]
    from _lab import TAG

    return subprocess.Popen(
        argv,
        env={**os.environ, "LAB_TAG": TAG, **stack.env, **e},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_load(proc: subprocess.Popen, duration: float) -> dict:
    out, err = proc.communicate(timeout=duration + 300)
    if proc.returncode != 0:
        raise RuntimeError(f"walcp_load rc {proc.returncode}: {err[-2000:]}")
    return json.loads(out.strip().splitlines()[-1])


def health(port: int) -> dict:
    return http_json("GET", port, "/internal/sqlite-health", timeout=10)["databases"]["default"]


def wait_caught_up(port: int, timeout: float = 300) -> dict:
    """부하가 끝난 뒤 헬스가 ``caught_up`` 이 될 때까지 기다린다.

    먼저 그 자리의 헬스를 한 번 남기고(``at_load_end``), 갱신 주기(2초) 두 번 + 1초를 쉰다. 쉬지
    않으면 부하 중에 갱신한 ``caught_up`` 을 그대로 읽어, 마지막 1초치가 올라가기 전에 복원하게
    된다(#106 시험 실행에서 꼬리 400행, 재현함).
    """
    try:
        first = health(port)
    except OSError as exc:
        first = {"status": f"error {type(exc).__name__}"}
    time.sleep(2 * float(HEALTH_ENV["LAB_HEALTH_REFRESH"]) + 1)
    t0 = time.monotonic()
    seen = [("at_load_end", first.get("status"), first.get("code"), first.get("age"))]
    while time.monotonic() - t0 < timeout:
        try:
            h = health(port)
        except OSError as exc:
            h = {"status": f"error {type(exc).__name__}"}
        seen.append((round(time.monotonic() - t0, 1), h.get("status"), h.get("code")))
        if h.get("status") == "caught_up":
            return {
                "seconds": round(time.monotonic() - t0, 1),
                "health": h,
                "at_load_end": first,
                "seen": seen[-5:],
            }
        time.sleep(1)
    return {"seconds": None, "timeout": timeout, "at_load_end": first, "seen": seen[-10:]}


def replica_check(stack: Stack, prefix: str, committed: list) -> dict:
    r = stack.replica(prefix, "--ids", timeout=900)
    out = {k: v for k, v in r.items() if k != "id_ranges"}
    if "id_ranges" in r:
        out.update(st.compare_ids(committed, r["id_ranges"]))
    return out


def log_lines(text: str) -> dict:
    lines = [ln[:300] for ln in text.splitlines() if LOG_NEEDLES.search(ln)]
    return {"count": len(lines), "lines": lines[:40], "tail": lines[-10:]}


def app_log_tail(text: str, n: int = 60) -> list[str]:
    """접근 로그 줄을 뺀 앱 로그의 끝(boot·sup·Litestream·예외 줄)."""
    keep = [ln[:300] for ln in text.splitlines() if '"GET ' not in ln and '"POST ' not in ln]
    return keep[-n:]


class Events:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, **detail) -> None:
        self.rows.append({"wall": time.time(), "name": name, **detail})
        log(f"event {name} {detail}")

    def relative(self, start_wall: float) -> list[dict]:
        return [{**r, "t": round(r["wall"] - start_wall, 2)} for r in self.rows]


def save(name: str, payload: dict) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"walcp-{RUN_ID}-{name}.json"
    path.write_text(json.dumps(payload, indent=1))
    return str(path)


def lsctl(stack: Stack, service: str, *args: str) -> dict:
    res = stack.compose("exec", "-T", service, "python", "/app/lab_tools/lsctl.py", *args)
    return json.loads(res.out.strip().splitlines()[-1])


def pid1(stack: Stack, service: str) -> str:
    res = stack.compose(
        "exec", "-T", service, "sh", "-c", "tr '\\0' ' ' < /proc/1/cmdline", check=False
    )
    return res.out.strip()


# --- Q1·Q2 -------------------------------------------------------------------------------------


def one_replica_run(stack, run_id: str, ckpt: int, mode: str, rep: int, version: str) -> dict:
    service = "labapp" if mode == "steady" else "labapp-sup"
    prefix = f"walcp-{mode}-{ckpt}-{rep}-{run_id}"
    volume = Volumes.take()
    # restart-persist: restart 와 같고 CONN_MAX_AGE=None. 0 이면 Litestream 이 없는 동안
    # 마지막 연결이 닫힐 때 -wal 이 지워질 수 있다. 연결을 쥐고 있으면 WAL 파일이 남아
    # 재시작만 일어난다(상류 #1540 코멘트의 better-sqlite3 꼴에 가깝다).
    cma = {"LAB_CONN_MAX_AGE": "none"} if mode == "restart-persist" else {}
    e = env(volume, prefix, "--init-new", **HEALTH_ENV, **pragmas_env(ckpt), **cma)
    port = stack.up_app(service, env=e)
    pragmas = check_ckpt(port, ckpt)
    cma_live = http_json("GET", port, "/lab/stats")["conn_max_age"]
    assert cma_live == (None if cma else 0), cma_live
    ev = Events()
    proc = start_load(stack, e, service, DURATION)
    ev.add("load_start")
    if mode.startswith("restart"):
        time.sleep(KILL_AT)
        ev.add("ls_stop", **lsctl(stack, service, "stop", "TERM"))
        time.sleep(HOLD)
        ev.add("ls_start", **lsctl(stack, service, "start"))
    load = finish_load(proc, DURATION)
    ev.add("load_end")
    caught = wait_caught_up(port)
    ev.add("caught_up", seconds=caught["seconds"])
    rep_live = replica_check(stack, prefix, load["committed_ranges"])
    ev.add("replica_live", missing=rep_live.get("missing_rows"))
    rc, took, logs = stack.stop_app(service)
    rep_stop = replica_check(stack, prefix, load["committed_ranges"])
    ev.add("replica_after_stop", missing=rep_stop.get("missing_rows"))
    events = ev.relative(load["start_wall"])
    run = {
        "litestream_version": version,
        "ckpt": ckpt,
        "mode": mode,
        "rep": rep,
        "conn_max_age": cma_live,
        "service": service,
        "volume": volume,
        "prefix": prefix,
        "pragmas": pragmas,
        "load_args": load["args"],
        "rows_committed": load["rows_committed"],
        "unknown_writes": load["unknown_writes"],
        "load_errors": load["errors"],
        "events": events,
        "caught_up": caught,
        "replica_live": rep_live,
        "replica_after_stop": rep_stop,
        "stop_rc": rc,
        "stop_s": round(took, 1),
        "wal": st.wal_summary(load["samples"]),
        "litestream_log": log_lines(logs),
        "samples": load["samples"],
        "intervals": load["intervals"],
    }
    if mode.startswith("restart"):
        down = [r["t"] for r in events if r["name"] in ("ls_stop", "ls_start")]
        if len(down) == 2:
            run["wal_while_down"] = st.wal_summary(load["samples"], down[0], down[1])
    log(
        f"walcp {version} ckpt={ckpt} {mode} rep {rep}: committed={load['rows_committed']} "
        f"live_missing={rep_live.get('missing_rows')} stop_missing={rep_stop.get('missing_rows')} "
        f"integrity={rep_live.get('integrity')}/{rep_stop.get('integrity')} "
        f"wal={run['wal']}"
    )
    return run


@pytest.mark.walcp
def test_walcp_replica(stack, run_id, rec, litestream_version):
    combos = [(c, m) for c in CKPTS for m in MODES]
    runs = []
    t0 = time.monotonic()
    for rep in range(1, REPS + 1):
        k = (rep - 1) % len(combos)
        for ckpt, mode in combos[k:] + combos[:k]:
            runs.append(one_replica_run(stack, run_id, ckpt, mode, rep, litestream_version))
    path = save(
        "replica",
        {
            "litestream_version": litestream_version,
            "reps": REPS,
            "duration_s": DURATION,
            "kill_at_s": KILL_AT,
            "hold_s": HOLD,
            "load": LOAD,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "runs": runs,
        },
    )
    rec["walcp_json"] = path
    rec["missing"] = [
        (r["ckpt"], r["mode"], r["rep"], r["replica_live"].get("missing_rows")) for r in runs
    ]
    # 측정이다: 빠진 행은 결과로 남기고 실패로 보지 않는다. 하네스가 끝까지 돈 것만 본다.
    assert len(runs) == REPS * len(combos)
    assert all("missing_rows" in r["replica_live"] for r in runs), [r["replica_live"] for r in runs]


@pytest.mark.walcp
def test_walcp_plain(stack, run_id, rec, litestream_version):
    runs = []
    for ckpt in CKPTS:
        volume = Volumes.take()
        e = env(volume, f"walcp-plain-{ckpt}-{run_id}", **pragmas_env(ckpt))
        port = stack.up_app("labapp-plain", env=e)
        pragmas = check_ckpt(port, ckpt)
        proc = start_load(stack, e, "labapp-plain", DURATION)
        load = finish_load(proc, DURATION)
        stack.stop_app("labapp-plain")
        runs.append(
            {
                "ckpt": ckpt,
                "pragmas": pragmas,
                "rows_committed": load["rows_committed"],
                "load_errors": load["errors"],
                "wal": st.wal_summary(load["samples"]),
                "samples": load["samples"],
                "intervals": load["intervals"],
            }
        )
        log(f"walcp plain ckpt={ckpt}: {runs[-1]['wal']}")
    rec["walcp_json"] = save("plain", {"load": LOAD, "duration_s": DURATION, "runs": runs})
    assert len(runs) == len(CKPTS)


# --- Q3 ----------------------------------------------------------------------------------------


def one_stall_run(stack, run_id: str, ckpt: int, kind: str, version: str) -> dict:
    service = "labapp" if kind == "hang" else "labapp-sup"
    prefix = f"walcp-stall-{kind}-{ckpt}-{run_id}"
    volume = Volumes.take()
    # dead-persist: dead 와 같고 CONN_MAX_AGE=None(연결 유지). 프로필 기본(0)은 요청마다 연결을 닫아
    # 마지막 연결이 닫힐 때 SQLite 가 체크포인트하고 -wal 을 지운다(wal_autocheckpoint 와 무관).
    cma = {"LAB_CONN_MAX_AGE": "none"} if kind == "dead-persist" else {}
    e = env(volume, prefix, "--init-new", **HEALTH_ENV, **pragmas_env(ckpt), **cma)
    port = stack.up_app(service, env=e)
    pragmas = check_ckpt(port, ckpt)
    cma_live = http_json("GET", port, "/lab/stats")["conn_max_age"]
    assert cma_live == (None if kind == "dead-persist" else 0), cma_live
    lead = 20.0
    duration = lead + STALL + 10
    ev = Events()
    first_pid = pid1(stack, service)
    proc = start_load(stack, e, service, duration)
    ev.add("load_start")
    time.sleep(lead)
    if kind == "hang":
        stack.compose("kill", "-s", "SIGSTOP", service)
        ev.add("ls_stop", how="SIGSTOP pid 1", pid1=first_pid)
    else:
        ev.add("ls_stop", **lsctl(stack, service, "stop", "TERM"))
    health_during = []
    t_end = time.monotonic() + STALL
    while time.monotonic() < t_end:
        try:
            h = health(port)
            health_during.append([round(time.time(), 1), h.get("status"), h.get("code")])
        except OSError as exc:
            health_during.append([round(time.time(), 1), f"error {type(exc).__name__}", None])
        time.sleep(10)
    disk = stack.compose("exec", "-T", service, "df", "-k", "/data", check=False).text.strip()
    ev.add("disk_before_resume", df=disk.splitlines()[-1:])
    if kind == "hang":
        stack.compose("kill", "-s", "SIGCONT", service)
        ev.add("ls_start", how="SIGCONT pid 1")
    else:
        ev.add("ls_start", **lsctl(stack, service, "start"))
    load = finish_load(proc, duration)
    ev.add("load_end")
    caught = wait_caught_up(port, timeout=600)
    ev.add("caught_up", seconds=caught["seconds"])
    rep_live = replica_check(stack, prefix, load["committed_ranges"])
    rc, took, logs = stack.stop_app(service)
    events = ev.relative(load["start_wall"])
    t_stop = next(r["t"] for r in events if r["name"] == "ls_stop")
    t_start = next(r["t"] for r in events if r["name"] == "ls_start")
    run = {
        "litestream_version": version,
        "ckpt": ckpt,
        "kind": kind,
        "conn_max_age": cma_live,
        "service": service,
        "prefix": prefix,
        "pragmas": pragmas,
        "pid1": first_pid,
        "load_args": load["args"],
        "rows_committed": load["rows_committed"],
        "load_errors": load["errors"],
        "events": events,
        "stall_s": round(t_start - t_stop, 1),
        "wal_before": st.wal_summary(load["samples"], 0, t_stop),
        "wal_during": st.wal_summary(load["samples"], t_stop, t_start),
        "wal_after": st.wal_summary(load["samples"], t_start, None),
        "growth_mb_per_min": st.growth_mb_per_min(load["samples"], t_stop, t_start),
        "latency_before": st.window_latency(load["intervals"], 0, t_stop, INTERVAL),
        "latency_during": st.window_latency(load["intervals"], t_stop, t_start, INTERVAL),
        "health_during": [[round(w - load["start_wall"], 1), s, c] for w, s, c in health_during],
        "caught_up": caught,
        "replica_live": rep_live,
        "stop_rc": rc,
        "litestream_log": log_lines(logs),
        "app_log_tail": app_log_tail(logs),
        "samples": load["samples"],
        "intervals": load["intervals"],
    }
    log(
        f"walcp stall {version} ckpt={ckpt} {kind}: growth={run['growth_mb_per_min']} MB/min "
        f"wal_during={run['wal_during']} missing={rep_live.get('missing_rows')}"
    )
    return run


@pytest.mark.walcp
def test_walcp_stall(stack, run_id, rec, litestream_version):
    runs = [
        one_stall_run(stack, run_id, ckpt, kind, litestream_version)
        for kind in STALL_KINDS
        for ckpt in CKPTS
    ]
    rec["walcp_json"] = save(
        "stall",
        {"litestream_version": litestream_version, "stall_s": STALL, "load": LOAD, "runs": runs},
    )
    assert len(runs) == len(CKPTS) * len(STALL_KINDS)


# --- Q4 ----------------------------------------------------------------------------------------


def levels(stack: Stack, prefix: str) -> dict:
    res = stack.s3_objects(prefix, "levels")
    return json.loads(res.out.strip().splitlines()[-1])["levels"]


@pytest.mark.walcp
def test_walcp_l0(stack, run_id, rec, litestream_version):
    prefix = f"walcp-l0-{run_id}"
    volume = Volumes.take()
    e = env(volume, prefix, "--init-new", **HEALTH_ENV)
    port = stack.up_app("labapp", env=e)
    ev = Events()
    t0 = time.time()

    def write(n: int = 5) -> None:
        http_json("POST", port, f"/lab/write?n={n}", timeout=30)

    # 몇 개의 L0 를 만든다(쓰기마다 1초 쉬어 L0 가 따로 생기게)
    for _ in range(10):
        write()
        time.sleep(1.2)
    before = levels(stack, prefix)
    ev.add("levels_before", levels=before)
    last_l0 = before.get("0", {}).get("last")
    assert last_l0, before
    if L0_TARGET == "remote":
        path = f"/buckets/dso-lab/{prefix}/0000/{last_l0}"
        res = stack.s3_objects(prefix, "corrupt-path", path)
    else:
        # 로컬 메타(.app.sqlite3-litestream) 아래 가장 최근 L0 파일 하나
        res = stack.compose("exec", "-T", "labapp", "python", "-c", LOCAL_CORRUPT, timeout=60)
    corrupt = json.loads(res.out.strip().splitlines()[-1])
    ev.add("corrupt", target=L0_TARGET, **corrupt)
    series = []
    deadline = time.monotonic() + L0_MINUTES * 60
    while time.monotonic() < deadline:
        for _ in range(10):
            write()
            time.sleep(1)
        try:
            h = health(port)
        except OSError as exc:
            h = {"status": f"error {type(exc).__name__}"}
        remote = stack.replica(prefix, "--no-restore", timeout=120)
        lv = levels(stack, prefix)
        row = {
            "t": round(time.time() - t0, 1),
            "levels": {k: v["count"] for k, v in lv.items()},
            "bytes": {k: v["bytes"] for k, v in lv.items()},
            "health": [h.get("status"), h.get("code")],
            "remote": remote,
            "local_rows": http_json("GET", port, "/lab/count")["count"],
        }
        series.append(row)
        log(f"walcp l0: {row}")
    restore = replica_check(stack, prefix, [])
    rc, _, logs = stack.stop_app("labapp")
    errors = [ln[:300] for ln in logs.splitlines() if re.search(r"level=(WARN|ERROR)", ln)]
    rec["walcp_json"] = save(
        "l0",
        {
            "litestream_version": litestream_version,
            "minutes": L0_MINUTES,
            "target": L0_TARGET,
            "events": ev.relative(t0),
            "series": series,
            "restore_end": restore,
            "warn_error_lines": len(errors),
            "warn_error_first": errors[:20],
            "warn_error_last": errors[-10:],
            "litestream_log": log_lines(logs),
        },
    )
    assert series
