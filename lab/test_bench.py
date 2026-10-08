"""PRAGMA 벤치 (DESIGN §6-0 후보). ``scripts/lab.sh bench``.

후보 PRAGMA 를 하나씩 켠 경우·모두 켠 경우를 기준(권장 설정만)과 비교한다. 반복 r 마다 모든
변형을 돌리고(순서는 r 만큼 회전), 같은 반복 안의 기준과 짝지어 차이를 잰다. 변형마다 새 볼륨·새
복제본 prefix 로 시작하고, 같은 시드로 같은 행을 채운 뒤 부하를 건다. Litestream 은 프로필 그대로
복제한다(운영과 같은 조건).

환경 변수: LAB_BENCH_REPS(5), LAB_BENCH_DURATION(20초), LAB_BENCH_ROWS(100000),
LAB_BENCH_CONCURRENCY(16). 결과: lab/.out/bench-<run>.json
"""

from __future__ import annotations

import json
import os
import statistics
import time

import pytest
from _lab import http_json, log
from conftest import OUT, RUN_ID
from test_scenarios import Volumes, env

VARIANTS = {
    "baseline": {},
    "temp_store": {"temp_store": "MEMORY"},
    "mmap_size": {"mmap_size": 268435456},  # 256 MiB
    "cache_size": {"cache_size": -65536},  # 64 MiB
    "journal_size_limit": {"journal_size_limit": 67108864},  # 64 MiB
}
VARIANTS["all"] = {k: v for d in VARIANTS.values() for k, v in d.items()}

REPS = int(os.environ.get("LAB_BENCH_REPS", "5"))
DURATION = float(os.environ.get("LAB_BENCH_DURATION", "20"))
ROWS = int(os.environ.get("LAB_BENCH_ROWS", "100000"))
CONCURRENCY = int(os.environ.get("LAB_BENCH_CONCURRENCY", "16"))
METRICS = ("rps", "p99_ms", "p50_ms", "wal_max")


def one_run(stack, run_id: str, name: str, rep: int) -> dict:
    prefix = f"bench-{name.replace('_', '')}-{rep}-{run_id}"
    volume = Volumes.take()
    pragmas = VARIANTS[name]
    e = env(volume, prefix, "--init-new", LAB_PRAGMAS=json.dumps(pragmas) if pragmas else "")
    port = stack.up_app(env=e)
    stats = http_json("GET", port, "/lab/stats")
    for key, value in pragmas.items():
        got = stats["pragmas"][key]
        want = {"MEMORY": 2}.get(value, value)
        assert got == want, (name, key, got, want)
    stack.compose("exec", "-T", "labapp", "python", "manage.py", "lab_seed", str(ROWS), timeout=600)
    time.sleep(5)  # 씨앗 쓰기의 복제가 잦아들 때까지
    res = stack.compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "loadgen",
        "--url",
        "http://labapp:8000",
        "--duration",
        str(DURATION),
        "--concurrency",
        str(CONCURRENCY),
        "--max-id",
        str(ROWS),
        "--seed",
        str(rep),
        env=e,
        timeout=DURATION + 120,
    )
    result = json.loads(res.out.strip().splitlines()[-1])
    result.update(variant=name, rep=rep, pragmas=pragmas, sizes_before=stats["sizes"])
    stack.stop_app()
    log(
        f"bench {name} rep {rep}: rps={result['rps']} p99={result['p99_ms']} "
        f"wal_max={result['wal_max']} errors={result['errors']}"
    )
    return result


def summarize(runs: list[dict]) -> dict:
    by = {name: [r for r in runs if r["variant"] == name] for name in VARIANTS}
    base = {r["rep"]: r for r in by["baseline"]}
    summary = {}
    for name, rows in by.items():
        entry: dict = {}
        for m in METRICS:
            vals = [r[m] for r in rows if r[m] is not None]
            entry[m] = {
                "mean": round(statistics.fmean(vals), 2) if vals else None,
                "stdev": round(statistics.stdev(vals), 2) if len(vals) > 1 else None,
                "values": vals,
            }
            if name != "baseline":
                deltas = [
                    round(100 * (r[m] - base[r["rep"]][m]) / base[r["rep"]][m], 1)
                    for r in rows
                    if r[m] is not None and base.get(r["rep"], {}).get(m)
                ]
                entry[m]["delta_pct_per_rep"] = deltas
                # 재현: 모든 반복에서 같은 방향이고, 가장 작은 차이도 5% 이상
                entry[m]["reproduced"] = bool(deltas) and (
                    all(d >= 5 for d in deltas) or all(d <= -5 for d in deltas)
                )
        entry["errors"] = sum(r["errors"] for r in rows)
        summary[name] = entry
    return summary


@pytest.mark.bench
def test_pragma_bench(stack, run_id, rec):
    names = list(VARIANTS)
    runs = []
    for rep in range(1, REPS + 1):
        order = names[rep - 1 :] + names[: rep - 1]
        for name in order:
            runs.append(one_run(stack, run_id, name, rep))
    summary = summarize(runs)
    out = {
        "reps": REPS,
        "duration_s": DURATION,
        "rows": ROWS,
        "concurrency": CONCURRENCY,
        "variants": VARIANTS,
        "summary": summary,
        "runs": runs,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"bench-{RUN_ID}.json"
    path.write_text(json.dumps(out, indent=1))
    rec["bench_json"] = str(path)
    rec["summary"] = {
        n: {m: (s[m]["mean"], s[m]["stdev"], s[m].get("reproduced")) for m in METRICS}
        for n, s in summary.items()
    }
    assert all(s["errors"] == 0 for s in summary.values()), summary
