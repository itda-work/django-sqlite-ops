"""PRAGMA 벤치 (DESIGN §6-0 후보, #10 → #26 재측정). ``scripts/lab.sh bench``.

두 테스트가 있다.

``test_pragma_bench`` — 후보 PRAGMA 변형을 ``CONN_MAX_AGE`` 두 값(0, None)에서 기준(권장 설정만)과
비교한다. 반복 r 마다 (CONN_MAX_AGE, 변형) 조합을 모두 돌고(순서는 r 만큼 회전), 같은 반복의 기준과
짝지어 차이를 잰다. 실행마다 새 볼륨·새 복제본 prefix 로 ``--init-new`` 부팅 → 같은 시드의 행을 채움
→ 쉼 → 씨앗 직후 WAL 표본 → 두 단계 부하:

- ``mixed``: #10 과 같은 비율(읽기 70%·정렬 10%·1행 쓰기 20%).
- ``write``: 쓰기를 늘린 변형(쓰기 ``LAB_BENCH_WRITE_SHARE``, 요청당 ``LAB_BENCH_WRITE_ROWS`` 행).
  측정 구간 동안 WAL 이 씨앗 뒤 크기보다 자라야 ``journal_size_limit`` 이 일할 여지가 생긴다.

``test_saturation`` — 기준 설정에서 동시 수를 바꿔 처리량 곡선을 재고, DB 없는 경로
(``ping``: Django, ``rawping``: Django 앞 ASGI)로 발생기·스택의 한계를 잰다(#10 리뷰 1: 병목 위치).

반복이 끝날 때마다 랩 스택을 ``down -v`` 로 내리고 다시 띄운다(볼륨 풀 40개를 다시 쓴다).
Litestream 은 프로필 그대로 복제한다(운영과 같은 조건).

환경 변수: LAB_BENCH_REPS(5), LAB_BENCH_DURATION(20초), LAB_BENCH_ROWS(100000),
LAB_BENCH_CONCURRENCY(16), LAB_BENCH_VARIANTS(쉼표, 기본 아래 BENCH_VARIANTS),
LAB_BENCH_CMA("0,none"), LAB_BENCH_WRITE_SHARE(0.7), LAB_BENCH_WRITE_ROWS(50),
LAB_BENCH_SAT_REPS(2), LAB_BENCH_SAT_DURATION(10초), LAB_BENCH_SAT_LEVELS("4,16,48").
결과: lab/.out/bench-<run>.json, lab/.out/saturation-<run>.json
"""

from __future__ import annotations

import json
import os
import time

import pytest
from _benchstat import metric, summarize
from _lab import http_json, log
from conftest import OUT, RUN_ID
from test_scenarios import Volumes, env

ALL_VARIANTS = {
    "baseline": {},
    "temp_store": {"temp_store": "MEMORY"},
    "mmap_size": {"mmap_size": 268435456},  # 256 MiB
    "cache_size": {"cache_size": -65536},  # 64 MiB
    "journal_size_limit": {"journal_size_limit": 67108864},  # 64 MiB
}
ALL_VARIANTS["all"] = {k: v for d in ALL_VARIANTS.values() for k, v in d.items()}
# #26 기본: 이 이슈가 묻는 후보만(연결 수명 → cache·mmap, WAL → journal_size_limit). 실행 시간을
# 1시간 안에 두려고 temp_store 와 '넷 다' 는 뺐다(결과 문서에 적는다).
BENCH_VARIANTS = ("baseline", "mmap_size", "cache_size", "journal_size_limit")

REPS = int(os.environ.get("LAB_BENCH_REPS", "5"))
DURATION = float(os.environ.get("LAB_BENCH_DURATION", "20"))
ROWS = int(os.environ.get("LAB_BENCH_ROWS", "100000"))
CONCURRENCY = int(os.environ.get("LAB_BENCH_CONCURRENCY", "16"))
VARIANTS = tuple(os.environ.get("LAB_BENCH_VARIANTS", ",".join(BENCH_VARIANTS)).split(","))
CMAS = tuple(os.environ.get("LAB_BENCH_CMA", "0,none").split(","))
WRITE_SHARE = float(os.environ.get("LAB_BENCH_WRITE_SHARE", "0.7"))
WRITE_ROWS = int(os.environ.get("LAB_BENCH_WRITE_ROWS", "50"))
SAT_REPS = int(os.environ.get("LAB_BENCH_SAT_REPS", "2"))
SAT_DURATION = float(os.environ.get("LAB_BENCH_SAT_DURATION", "10"))
SAT_LEVELS = tuple(int(c) for c in os.environ.get("LAB_BENCH_SAT_LEVELS", "4,16,48").split(","))

PHASES = {
    "mixed": {"read": 0.7, "sorted": 0.1, "write_rows": 1},
    "write": {"read": 1 - WRITE_SHARE, "sorted": 0.0, "write_rows": WRITE_ROWS},
}
METRICS = (
    "rps",
    "p99_ms",
    "p50_ms",
    "svc.p50_ms",
    "svc.p99_ms",
    "app.p50_ms",
    "server.server_cpu_util",
    "gen_cpu_util",
    "server.conn_created",
    "server.db_requests",
    "server.conn_per_db_request",
    "server.db_fds_end",
    "wal_start",
    "wal_max",
    "wal_end",
    "server.wal_restarts",
    "wal_shrinks",
)
PAIRED = ("rps", "p99_ms", "p50_ms", "svc.p50_ms", "wal_max", "wal_end")


def recycle(stack) -> None:
    """랩 스택을 내리고(볼륨 포함) 다시 띄운다. 우리 compose 프로젝트만 대상이다."""
    stack.down()
    Volumes._next = 1
    stack.up_infra()


def boot(stack, prefix: str, pragmas: dict, cma: str) -> tuple[dict, int]:
    volume = Volumes.take()
    e = env(
        volume,
        prefix,
        "--init-new",
        LAB_PRAGMAS=json.dumps(pragmas) if pragmas else "",
        LAB_CONN_MAX_AGE=cma,
    )
    port = stack.up_app(env=e)
    stats = http_json("GET", port, "/lab/stats")
    for key, value in pragmas.items():
        got = stats["pragmas"][key]
        want = {"MEMORY": 2}.get(value, value)
        assert got == want, (prefix, key, got, want)
    want_cma = None if cma == "none" else int(cma)
    assert stats["conn_max_age"] == want_cma, (prefix, stats["conn_max_age"], cma)
    return e, port


def seed(stack, port: int) -> dict:
    stack.compose("exec", "-T", "labapp", "python", "manage.py", "lab_seed", str(ROWS), timeout=600)
    time.sleep(5)  # 씨앗 쓰기의 복제가 잦아들 때까지
    return http_json("GET", port, "/lab/probe")


def load(stack, e: dict, *, mode: str, duration: float, concurrency: int, seed_n: int, **kw):
    argv = [
        "--url",
        "http://labapp:8000",
        "--mode",
        mode,
        "--duration",
        str(duration),
        "--concurrency",
        str(concurrency),
        "--max-id",
        str(ROWS),
        "--seed",
        str(seed_n),
    ]
    for key, value in kw.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    res = stack.compose(
        "run", "--rm", "--no-deps", "-T", "loadgen", *argv, env=e, timeout=duration + 120
    )
    return json.loads(res.out.strip().splitlines()[-1])


def error_log(stack, results) -> str | None:
    """오류가 난 단계가 있으면 앱 로그의 끝(오류 원인)을 돌려준다."""
    if not any(r["errors"] for r in results):
        return None
    return stack.logs("labapp")[-6000:]


def one_run(stack, run_id: str, cma: str, name: str, rep: int) -> dict:
    prefix = f"bench-{cma}-{name.replace('_', '')}-{rep}-{run_id}"
    pragmas = ALL_VARIANTS[name]
    e, port = boot(stack, prefix, pragmas, cma)
    after_seed = seed(stack, port)
    phases = {}
    for phase, mix in PHASES.items():
        phases[phase] = load(
            stack, e, mode="mixed", duration=DURATION, concurrency=CONCURRENCY, seed_n=rep, **mix
        )
    final = http_json("GET", port, "/lab/stats")
    app_log = error_log(stack, phases.values())
    stack.stop_app()
    run = {
        "cma": cma,
        "variant": name,
        "rep": rep,
        "pragmas": pragmas,
        "after_seed": after_seed,
        "sizes_end": final["sizes"],
        "phases": phases,
        "app_log_errors": app_log,
    }
    for phase in PHASES:
        log(
            f"bench {cma} {name} rep {rep} {phase}: rps={metric(run, phase, 'rps')} "
            f"p99={metric(run, phase, 'p99_ms')} svc50={metric(run, phase, 'svc.p50_ms')} "
            f"conn/req={metric(run, phase, 'server.conn_per_db_request')} "
            f"wal={metric(run, phase, 'wal_start')}/{metric(run, phase, 'wal_max')}/"
            f"{metric(run, phase, 'wal_end')} restarts={metric(run, phase, 'server.wal_restarts')} "
            f"errors={metric(run, phase, 'errors')} {run['phases'][phase]['error_kinds']}"
        )
    return run


@pytest.mark.bench
def test_pragma_bench(stack, run_id, rec):
    combos = [(cma, name) for cma in CMAS for name in VARIANTS]
    runs = []
    t0 = time.monotonic()
    for rep in range(1, REPS + 1):
        if rep > 1:
            recycle(stack)
        k = (rep - 1) % len(combos)
        for cma, name in combos[k:] + combos[:k]:
            runs.append(one_run(stack, run_id, cma, name, rep))
    summary = summarize(
        runs, cmas=CMAS, variants=VARIANTS, phases=PHASES, metrics=METRICS, paired=PAIRED, reps=REPS
    )
    out = {
        "reps": REPS,
        "duration_s": DURATION,
        "rows": ROWS,
        "concurrency": CONCURRENCY,
        "cma": CMAS,
        "variants": {n: ALL_VARIANTS[n] for n in VARIANTS},
        "phases": PHASES,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "summary": summary,
        "runs": runs,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"bench-{RUN_ID}.json"
    path.write_text(json.dumps(out, indent=1))
    rec["bench_json"] = str(path)
    errors = {
        (cma, phase, v): s["errors"]
        for cma, by_phase in summary.items()
        for phase, by_variant in by_phase.items()
        for v, s in by_variant.items()
    }
    rec["errors"] = {"/".join(k): n for k, n in errors.items() if n}
    # CONN_MAX_AGE=None 은 ASGI 에서 연결이 쌓여 fd 한도에 닿으면 500 이 난다(#26 에서 재현).
    # 그 오류는 결과로 기록하고, 실패 판정은 프로필 기본(0)에만 건다.
    assert not any(n for (cma, _, _), n in errors.items() if cma != "none"), errors


@pytest.mark.bench
def test_saturation(stack, run_id, rec):
    """기준 설정의 동시 수별 처리량 곡선과 DB 없는 경로의 한계(발생기 여유)."""
    rows = []
    t0 = time.monotonic()
    for rep in range(1, SAT_REPS + 1):
        recycle(stack)
        for cma in CMAS:
            prefix = f"sat-{cma}-{rep}-{run_id}"
            e, port = boot(stack, prefix, {}, cma)
            seed(stack, port)
            steps = [("rawping", c) for c in (16, max(SAT_LEVELS))]
            steps += [("ping", c) for c in (16, max(SAT_LEVELS))]
            steps += [("mixed", c) for c in SAT_LEVELS]
            results = []
            for mode, c in steps:
                res = load(
                    stack, e, mode=mode, duration=SAT_DURATION, concurrency=c, seed_n=rep, warmup=2
                )
                res.pop("wal_series", None)
                rows.append({"rep": rep, "cma": cma, "mode": mode, "concurrency": c, **res})
                results.append(res)
                log(
                    f"sat {cma} rep {rep} {mode} c={c}: rps={res['rps']} p50={res['p50_ms']} "
                    f"svc50={res['svc']['p50_ms']} app50={res['app']['p50_ms']} "
                    f"gen_cpu={res['gen_cpu_util']} "
                    f"srv_cpu={res['server'].get('server_cpu_util')} "
                    f"fds={res['server'].get('db_fds_max')} errors={res['errors']} "
                    f"{res['error_kinds']}"
                )
            log_tail = error_log(stack, results)
            if log_tail:
                rows[-1]["app_log_errors"] = log_tail
            stack.stop_app()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"saturation-{RUN_ID}.json"
    path.write_text(
        json.dumps(
            {
                "reps": SAT_REPS,
                "duration_s": SAT_DURATION,
                "levels": SAT_LEVELS,
                "elapsed_s": round(time.monotonic() - t0, 1),
                "rows": rows,
            },
            indent=1,
        )
    )
    rec["saturation_json"] = str(path)
    bad = [r for r in rows if r["errors"] and r["cma"] != "none"]  # None 은 기록만(위와 같음)
    assert not bad, bad
