"""soak: fd 한도를 올린 ASGI 에서 ``CONN_MAX_AGE`` None·0 장시간 실행(#33). ``scripts/lab.sh soak``.

#26 에서 ASGI 동기 뷰 + ``CONN_MAX_AGE=None`` 은 연결을 쌓아 fd 소프트 한도(1024) 근처에서 500 을
냈다. 여기서는 열린 파일 한도를 컨테이너가 허용하는 최대(``fs.nr_open``)로 올리고 메모리를 막은
``labapp-soak``(lab-compose.yaml)에서 두 값을 같은 부하로 오래 돌려, fd·RSS 가 요청 수에 비례해
계속 느는지, 어디서 멈추는지를 원자료로 남긴다.

실행마다 스택을 새로 띄우고(``down -v`` 후 ``up``) 새 볼륨·새 복제본 prefix 로 ``--init-new`` 부팅
→ 행 ``LAB_SOAK_ROWS`` 개 → ``loadgen --report-every`` 를 ``LAB_SOAK_DURATION`` 초. 부하는 PRAGMA
벤치의 ``mixed`` 와 같은 비율이다(읽기 70%·정렬 10%·1행 쓰기 20%). 구간 줄은 오는 대로
``lab/.out/soak-<run>.jsonl`` 에 쓴다. loadgen 이 안전 정지 조건(``stop_reason``)에 걸리면 그 실행을
멈추고 사유를 남긴다. 컨테이너 메모리 한도(``LAB_SOAK_MEM``, 스왑 없음)가 마지막 방어선이다.

기본 랩 실행(``run``)과 ``bench`` 에는 섞이지 않는다(마커 ``soak``).

환경 변수: LAB_SOAK_DURATION(1800초), LAB_SOAK_INTERVAL(10초), LAB_SOAK_CONCURRENCY(16),
LAB_SOAK_ROWS(100000), LAB_SOAK_CMA("none,0"), LAB_SOAK_MEM(2g), LAB_SOAK_NOFILE(1048576),
LAB_SOAK_STOP_MEM_FRAC(0.8), LAB_SOAK_STOP_ERR_RATE(0.5), LAB_SOAK_STOP_ERR_INTERVALS(3).
결과: lab/.out/soak-<run>.jsonl(구간 줄·환경·최종), lab/.out/soak-<run>-summary.json
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time

import pytest
from _lab import OUT, RUN_ID, TAG, error_log, http_json, log
from _soakstat import analyze, compare, harness_problems, nofile_limits
from test_scenarios import Volumes, env

SERVICE = "labapp-soak"
DURATION = float(os.environ.get("LAB_SOAK_DURATION", "1800"))
INTERVAL = float(os.environ.get("LAB_SOAK_INTERVAL", "10"))
CONCURRENCY = int(os.environ.get("LAB_SOAK_CONCURRENCY", "16"))
ROWS = int(os.environ.get("LAB_SOAK_ROWS", "100000"))
CMAS = tuple(os.environ.get("LAB_SOAK_CMA", "none,0").split(","))
MEM = os.environ.get("LAB_SOAK_MEM", "2g")
NOFILE = os.environ.get("LAB_SOAK_NOFILE", "1048576")
STOP_MEM_FRAC = float(os.environ.get("LAB_SOAK_STOP_MEM_FRAC", "0.8"))
STOP_ERR_RATE = float(os.environ.get("LAB_SOAK_STOP_ERR_RATE", "0.5"))
STOP_ERR_INTERVALS = int(os.environ.get("LAB_SOAK_STOP_ERR_INTERVALS", "3"))
MIX = {"read": 0.7, "sorted": 0.1, "write_rows": 1}  # 벤치 mixed 와 같다
# 발생기 기한 = DURATION + 이 값. 넘으면 출력이 없어도 죽이고 'harness timeout' 으로 남긴다.
HARNESS_GRACE = 600.0
KILL_DRAIN_S = 30.0


def read_loadgen(proc, limit_s: float, on_row) -> dict:
    """발생기의 stdout(JSON 줄)·stderr 를 스레드로 읽으며 기한을 따로 잰다(리뷰 1).

    줄을 기다리는 동안에도 기한을 본다. 출력이 없거나, 줄이 끝나지 않거나, JSON 이 아닌 줄만
    나와도 기한이 지나면 ``proc.kill()`` 하고 ``timed_out`` 을 참으로 돌려준다. kill 뒤에도
    파이프가 ``KILL_DRAIN_S`` 안에 닫히지 않으면 기다리지 않고 돌아간다(정리는 ``down`` 이 한다).
    """
    q: queue.Queue = queue.Queue()

    def pump(stream, tag: str) -> None:
        try:
            for line in stream:
                q.put((tag, line))
        finally:
            q.put((tag, None))

    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=pump, args=(stream, tag), daemon=True).start()
    deadline = time.monotonic() + limit_s
    open_streams, timed_out = 2, False
    err: collections.deque = collections.deque(maxlen=200)
    nonjson: list[str] = []
    while open_streams:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if timed_out:
                break
            proc.kill()  # docker compose CLI 만 끊긴다. 컨테이너는 down 이 지운다.
            timed_out = True
            deadline = time.monotonic() + KILL_DRAIN_S
            continue
        try:
            tag, line = q.get(timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        if line is None:
            open_streams -= 1
        elif tag == "err":
            err.append(line)
        elif line.strip():
            try:
                row = json.loads(line)
            except ValueError:
                nonjson.append(line.strip()[:300])
                continue
            on_row(row)
    try:
        rc = proc.wait(timeout=KILL_DRAIN_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = None
    return {"rc": rc, "timed_out": timed_out, "stderr": "".join(err), "nonjson": nonjson}


def soak_one(stack, run_id: str, cma: str, jsonl) -> dict:
    stack.down()
    Volumes._next = 1
    stack.up_infra()
    prefix = f"soak-{cma}-{run_id}"
    e = env(
        Volumes.take(),
        prefix,
        "--init-new",
        LAB_CONN_MAX_AGE=cma,
        LAB_SOAK_MEM=MEM,
        LAB_SOAK_NOFILE=NOFILE,
    )
    port = stack.up_app(SERVICE, env=e)
    senv = http_json("GET", port, "/lab/soakenv")
    want_cma = None if cma == "none" else int(cma)
    assert senv["conn_max_age"] == want_cma, senv["conn_max_age"]
    soft, hard = nofile_limits(senv["self_limits"])
    assert soft == hard == int(NOFILE), (soft, hard)
    mem_limit = senv["cgroup"]["max"]
    assert mem_limit, senv["cgroup"]  # 메모리 한도 없이 돌리지 않는다
    assert senv["cgroup"]["swap_max"] in (0, None), senv["cgroup"]
    jsonl.write(json.dumps({"cma": cma, "kind": "env", **senv}) + "\n")
    log(f"soak {cma}: nofile {soft}/{hard}, nr_open {senv['nr_open'].strip()}, mem {mem_limit}")

    stack.compose("exec", "-T", SERVICE, "python", "manage.py", "lab_seed", str(ROWS), timeout=600)
    time.sleep(5)  # 씨앗 쓰기의 복제가 잦아들 때까지
    # 부하 직전 표본(구간 줄과 같은 모양). 씨앗은 별도 프로세스(exec)라 앱 fd 에는 들어가지 않는다.
    baseline = http_json("GET", port, "/lab/soakprobe", timeout=30)
    jsonl.write(json.dumps({"cma": cma, "kind": "baseline", "probe": baseline}) + "\n")
    argv = [
        "--url",
        f"http://{SERVICE}:8000",
        "--duration",
        str(DURATION),
        "--concurrency",
        str(CONCURRENCY),
        "--max-id",
        str(ROWS),
        "--report-every",
        str(INTERVAL),
        "--stop-mem-frac",
        str(STOP_MEM_FRAC),
        "--stop-err-rate",
        str(STOP_ERR_RATE),
        "--stop-err-intervals",
        str(STOP_ERR_INTERVALS),
        *[a for k, v in MIX.items() for a in (f"--{k.replace('_', '-')}", str(v))],
    ]
    proc = subprocess.Popen(
        [*stack.argv(), "run", "--rm", "--no-deps", "-T", "loadgen", *argv],
        env={**os.environ, "LAB_TAG": TAG, **e},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    rows: list[dict] = []
    final: dict = {}

    def on_row(row: dict) -> None:
        nonlocal final
        kind = "final" if row.get("final") else "interval"
        jsonl.write(json.dumps({"cma": cma, "kind": kind, **row}) + "\n")
        jsonl.flush()
        if row.get("final"):
            final = row
            return
        rows.append(row)
        if row["i"] % 6 == 0 or row.get("tail"):
            p = row.get("probe") or {}
            fds = p.get("fds") or {}
            log(
                f"soak {cma} t={row['t_s']:.0f}s req={row['cum_requests']} rps={row['rps']} "
                f"p99={row['p99_ms']} err={row['errors']} {row['error_kinds']} "
                f"fd={fds.get('total')} db/wal/shm={fds.get('db')}/{fds.get('wal')}/"
                f"{fds.get('shm')} rss={p.get('VmRSS')} thr={p.get('Threads')}"
                + (" (tail)" if row.get("tail") else "")
            )

    got = read_loadgen(proc, DURATION + HARNESS_GRACE, on_row)
    rc, timed_out = got["rc"], got["timed_out"]
    if timed_out:
        log(f"soak {cma}: harness timeout after {DURATION + HARNESS_GRACE:.0f}s, loadgen killed")
    problems = harness_problems(
        rows, final, rc, duration=DURATION, interval=INTERVAL, timed_out=timed_out
    )
    state = stack.state(SERVICE)
    app_log = error_log(stack, [{"errors": final.get("cum_errors") or 0}], SERVICE)
    stack.stop_app(SERVICE)
    summary = analyze(rows, fd_limit=soft, mem_limit=mem_limit, baseline=baseline)
    summary.update(
        {
            "cma": cma,
            "stop_reason": final.get("stop_reason") or ("harness timeout" if timed_out else None),
            "harness_problems": problems,
            "loadgen_rc": rc,
            "loadgen_final": {k: v for k, v in final.items() if k != "error_sample"},
            "loadgen_stderr_tail": got["stderr"][-2000:],
            "loadgen_nonjson_stdout": got["nonjson"][-20:],
            "error_sample": final.get("error_sample"),
            "app_state": {k: state.get(k) for k in ("Status", "ExitCode", "OOMKilled")},
            "app_log_errors": app_log,
            "nofile": [soft, hard],
            "mem_limit": mem_limit,
        }
    )
    jsonl.write(json.dumps({"cma": cma, "kind": "summary", **summary}) + "\n")
    jsonl.flush()
    log(
        f"soak {cma} done: stop={summary['stop_reason']} req={summary['requests']} "
        f"err={summary['errors']} fd_end={summary.get('fd_end')} rss_end={summary.get('rss_end')}"
    )
    return summary


@pytest.mark.soak
def test_soak(stack, run_id, rec):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"soak-{RUN_ID}.jsonl"
    by_cma = {}
    t0 = time.monotonic()
    with path.open("w", encoding="utf-8") as jsonl:
        for cma in CMAS:
            by_cma[cma] = soak_one(stack, run_id, cma, jsonl)
    out = {
        "duration_s": DURATION,
        "interval_s": INTERVAL,
        "concurrency": CONCURRENCY,
        "rows": ROWS,
        "mix": MIX,
        "mem": MEM,
        "nofile": NOFILE,
        "stop": {
            "mem_frac": STOP_MEM_FRAC,
            "err_rate": STOP_ERR_RATE,
            "err_intervals": STOP_ERR_INTERVALS,
        },
        "elapsed_s": round(time.monotonic() - t0, 1),
        "compare": compare(by_cma),
        "by_cma": by_cma,
        "raw": path.name,
    }
    summary_path = OUT / f"soak-{RUN_ID}-summary.json"
    summary_path.write_text(json.dumps(out, indent=1))
    rec["soak_jsonl"] = str(path)
    rec["soak_summary"] = str(summary_path)
    # 하네스·발생기 실패는 CONN_MAX_AGE 와 관계없이 실패다(리뷰 1).
    bad = {cma: s["harness_problems"] for cma, s in by_cma.items() if s["harness_problems"]}
    assert not bad, f"harness problems: {bad}"
    # None 의 오류·조기 정지는 결과로 기록한다. 실패 판정은 프로필 기본(0)에만 건다(벤치와 같다).
    for cma, s in by_cma.items():
        if cma != "none":
            assert not s["errors"] and not s["stop_reason"], (cma, s["errors"], s["stop_reason"])
