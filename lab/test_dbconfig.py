"""Django 기본 SQLite 대 ``sqlite_database()`` 권장 설정의 동시 쓰기 비교(#83).

``scripts/lab.sh dbconfig`` 로 돈다.

두 테스트가 있다.

``test_litestream_sets_wal`` — 프로필 그대로의 ``labapp``(boot → ``litestream replicate``)에 Django
기본 설정을 넣으면 journal_mode 가 무엇인지 남긴다. Litestream 이 WAL 로 바꾸면 그 스택으로는
Django 기본(rollback journal)을 잴 수 없다는 근거다.

``test_dbconfig_bench`` — ``labapp-plain``(boot·Litestream 없이 migrate → uvicorn)으로 두 설정을
같은 이미지·같은 부하 발생기·같은 머신에서 번갈아 돈다.

- ``django-default``: ``{"ENGINE": "django.db.backends.sqlite3", "NAME": ...}`` (OPTIONS 없음)
- ``recommended``: 프로필 문서의 ``sqlite_database(..., profile="single-server")`` 그대로

반복 r 마다 (워커 수, 설정) 조합을 모두 돌고 순서를 r 만큼 회전한다. 실행마다 새 볼륨 → 같은 시드의
행 → 3초 데움(``mixed``, 집계 안 함) → 세 단계 부하(각 ``LAB_DBCONFIG_DURATION`` 초, 워밍업 없음,
동시 ``LAB_DBCONFIG_CONCURRENCY``):

- ``mixed``: PRAGMA 벤치와 같다(읽기 70%·정렬 10%·1행 쓰기 20%).
- ``write``: 읽기 30%·50행 쓰기 70%.
- ``rmw``: 읽기 50%·읽고-고쳐-쓰기 50%(한 트랜잭션 안 ``get`` → ``save``).

``database is locked`` 는 단계마다 앱 로그의
``django.db.utils.OperationalError: database is locked`` 줄 수의 증가로 센다(500 응답 수와 따로).
집계는 ``_benchstat`` 의 짝 비교를 그대로 쓴다: 워커 수를 ``cma`` 자리에, 설정을 ``variant``
자리에(``baseline`` = Django 기본) 둔다.

환경 변수: LAB_DBCONFIG_REPS(5), LAB_DBCONFIG_DURATION(20초), LAB_DBCONFIG_ROWS(100000),
LAB_DBCONFIG_CONCURRENCY(16), LAB_DBCONFIG_WORKERS("1,4").
결과: lab/.out/dbconfig-<run>.json. 표·CSV 는 ``lab/dbconfig_report.py``.
"""

from __future__ import annotations

import collections
import json
import os
import re
import time

import pytest
from _benchstat import metric, summarize
from _lab import OUT, RUN_ID, http_json, log
from test_bench import load
from test_scenarios import Volumes, env

SERVICE = "labapp-plain"
CONFIGS = ("django-default", "recommended")
# _benchstat 의 기준(baseline) 자리에 Django 기본을 둔다
VARIANT = {"django-default": "baseline", "recommended": "recommended"}
EXPECT = {
    "django-default": {"journal_mode": "delete", "transaction_mode": None},
    "recommended": {"journal_mode": "wal", "transaction_mode": "IMMEDIATE"},
}
LOCKED = "django.db.utils.OperationalError: database is locked"

REPS = int(os.environ.get("LAB_DBCONFIG_REPS", "5"))
DURATION = float(os.environ.get("LAB_DBCONFIG_DURATION", "20"))
ROWS = int(os.environ.get("LAB_DBCONFIG_ROWS", "100000"))
CONCURRENCY = int(os.environ.get("LAB_DBCONFIG_CONCURRENCY", "16"))
WARMUP = 3.0
WORKERS = tuple(int(w) for w in os.environ.get("LAB_DBCONFIG_WORKERS", "1,4").split(","))

PHASES = {
    "mixed": {"read": 0.7, "sorted": 0.1, "rmw": 0.0, "write_rows": 1},
    "write": {"read": 0.3, "sorted": 0.0, "rmw": 0.0, "write_rows": 50},
    "rmw": {"read": 0.5, "sorted": 0.0, "rmw": 0.5, "write_rows": 1},
}
METRICS = (
    "rps",
    "p50_ms",
    "p99_ms",
    "errors",
    "locked",
    "kinds.read.p99_ms",
    "kinds.write.p99_ms",
    "kinds.rmw.p99_ms",
    "kinds.read.errors",
    "kinds.write.errors",
    "kinds.rmw.errors",
    "kinds.sorted.errors",
    "gen_cpu_util",
)
PAIRED = ("rps", "p50_ms", "p99_ms")


def exception_lines(stack) -> collections.Counter:
    """앱 로그의 예외 줄(``<이름>Error: <메시지>``)을 종류별로 센다(``_lab.error_log`` 와 같은
    꼴)."""
    return collections.Counter(
        line.strip()
        for line in stack.logs(SERVICE).splitlines()
        if re.match(r"^[\w.]+(Error|Exception|DoesNotExist): ", line)
    )


def check_stats(stats: dict, config: str) -> None:
    got = {
        "journal_mode": stats["pragmas"]["journal_mode"],
        "transaction_mode": stats["transaction_mode"],
    }
    assert got == EXPECT[config], (config, got)


def one_run(stack, run_id: str, workers: int, config: str, rep: int) -> dict:
    volume = Volumes.take()
    e = env(volume, f"dbconfig-{run_id}", LAB_DB_CONFIG=config, LAB_WORKERS=str(workers))
    port = stack.up_app(SERVICE, env=e)
    stats = http_json("GET", port, "/lab/stats")
    check_stats(stats, config)
    stack.compose("exec", "-T", SERVICE, "python", "manage.py", "lab_seed", str(ROWS), timeout=600)
    time.sleep(2)
    common = {
        "mode": "mixed",
        "concurrency": CONCURRENCY,
        "seed_n": rep,
        "url": f"http://{SERVICE}:8000",
        "max_id": ROWS,
    }
    # 데움: 집계하지 않는다. 단계는 워밍업 없이 재서 로그의 예외 줄과 응답이 같은 구간이 된다.
    load(stack, e, duration=WARMUP, warmup=0, **common, **PHASES["mixed"])
    phases = {}
    before = exception_lines(stack)
    for phase, mix in PHASES.items():
        res = load(stack, e, duration=DURATION, warmup=0, **common, **mix)
        res.pop("wal_series", None)
        after = exception_lines(stack)
        res["exceptions"] = dict(after - before)
        res["locked"] = res["exceptions"].get(LOCKED, 0)
        before = after
        phases[phase] = res
    final = http_json("GET", port, "/lab/stats")
    stack.stop_app(SERVICE)
    run = {
        "cma": f"w{workers}",
        "variant": VARIANT[config],
        "config": config,
        "workers": workers,
        "rep": rep,
        "volume": volume,
        "pragmas": stats["pragmas"],
        "transaction_mode": stats["transaction_mode"],
        "sizes_end": final["sizes"],
        "phases": phases,
    }
    for phase in PHASES:
        log(
            f"dbconfig w{workers} {config} rep {rep} {phase}: "
            f"rps={metric(run, phase, 'rps')} p99={metric(run, phase, 'p99_ms')} "
            f"errors={metric(run, phase, 'errors')} locked={metric(run, phase, 'locked')} "
            f"{run['phases'][phase]['error_kinds']}"
        )
    return run


@pytest.mark.dbconfig
def test_litestream_sets_wal(stack, run_id, rec):
    """프로필 그대로의 스택(boot → litestream replicate)에 Django 기본 설정을 넣은 경우."""
    volume = Volumes.take()
    e = env(volume, f"dbconfig-ls-{run_id}", "--init-new", LAB_DB_CONFIG="django-default")
    port = stack.up_app(env=e)
    stats = http_json("GET", port, "/lab/stats")
    stack.stop_app()
    rec["journal_mode"] = stats["pragmas"]["journal_mode"]
    rec["transaction_mode"] = stats["transaction_mode"]
    log(f"dbconfig litestream + django-default: {rec}")
    assert stats["transaction_mode"] is None, stats


@pytest.mark.dbconfig
def test_dbconfig_bench(stack, run_id, rec):
    combos = [(w, c) for w in WORKERS for c in CONFIGS]
    runs = []
    t0 = time.monotonic()
    for rep in range(1, REPS + 1):
        k = (rep - 1) % len(combos)
        for workers, config in combos[k:] + combos[:k]:
            runs.append(one_run(stack, run_id, workers, config, rep))
    cmas = tuple(f"w{w}" for w in WORKERS)
    summary = summarize(
        runs,
        cmas=cmas,
        variants=tuple(VARIANT.values()),
        phases=PHASES,
        metrics=METRICS,
        paired=PAIRED,
        reps=REPS,
    )
    out = {
        "reps": REPS,
        "duration_s": DURATION,
        "rows": ROWS,
        "concurrency": CONCURRENCY,
        "workers": WORKERS,
        "configs": CONFIGS,
        "phases": PHASES,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "summary": summary,
        "runs": runs,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"dbconfig-{RUN_ID}.json"
    path.write_text(json.dumps(out, indent=1))
    rec["dbconfig_json"] = str(path)
    # 오류는 결과다(기본 설정의 locked 를 재려는 측정). 하네스가 돈 것만 확인한다.
    assert len(runs) == REPS * len(combos)
