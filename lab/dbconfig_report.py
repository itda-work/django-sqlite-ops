"""기본 대 권장 비교 원자료(``dbconfig-<run>.json``)에서 결과 문서의 표와 작은 CSV 를 만든다(#83).

    uv run --no-project python lab/dbconfig_report.py lab/.out/dbconfig-<run>.json \\
        --csv docs/research/data/dbconfig-2026-10-26.csv > 표.md

결과 문서의 수치는 손으로 옮기지 않고 이 출력에서 가져온다. CSV 는 실행·단계마다 한 줄이고 커밋할
수 있는 크기다(차트는 ``docs/assets/charts.py`` 가 이 CSV 에서 만든다). 판정 규칙은 측정 전에
정했다: 처리량·지연은 ``_benchstat`` 의 짝 비교(5회 모두 같은 방향 5% 이상), locked 는 '기본은 모든
반복에서 > 0, 권장은 모든 반복에서 0'(``locked_verdict``). 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _benchstat import metric, paired_deltas, reproduced  # noqa: E402

CSV_FIELDS = (
    "rep",
    "workers",
    "config",
    "phase",
    "rps",
    "p50_ms",
    "p99_ms",
    "errors",
    "locked",
    "read_p99_ms",
    "write_p99_ms",
    "rmw_p99_ms",
    "rmw_errors",
    "journal_mode",
    "transaction_mode",
)
# 짝 비교·판정을 내는 지표(측정 전에 정한 규칙, ``_benchstat.reproduced``)
PAIRED = ("rps", "p50_ms", "p99_ms")
LABEL = {"django-default": "Django 기본", "recommended": "권장"}


def locked_verdict(default: list[int], recommended: list[int], reps: int) -> str:
    """locked 판정(측정 전에 정함). 반복 수만큼 값이 있어야 판정한다."""
    if len(default) != reps or len(recommended) != reps:
        return "판정 불가"
    if all(n > 0 for n in default) and all(n == 0 for n in recommended):
        return "재현"
    return "재현 안 됨"


def csv_rows(data: dict) -> list[dict]:
    rows = []
    for run in sorted(data["runs"], key=lambda r: (r["rep"], r["workers"], r["config"])):
        for phase in data["phases"]:
            rows.append(
                {
                    "rep": run["rep"],
                    "workers": run["workers"],
                    "config": run["config"],
                    "phase": phase,
                    "rps": metric(run, phase, "rps"),
                    "p50_ms": metric(run, phase, "p50_ms"),
                    "p99_ms": metric(run, phase, "p99_ms"),
                    "errors": metric(run, phase, "errors"),
                    "locked": metric(run, phase, "locked"),
                    "read_p99_ms": metric(run, phase, "kinds.read.p99_ms"),
                    "write_p99_ms": metric(run, phase, "kinds.write.p99_ms"),
                    "rmw_p99_ms": metric(run, phase, "kinds.rmw.p99_ms"),
                    "rmw_errors": metric(run, phase, "kinds.rmw.errors"),
                    "journal_mode": run["pragmas"]["journal_mode"],
                    "transaction_mode": run["transaction_mode"] or "",
                }
            )
    return rows


def fmt(v, digits=1) -> str:
    if v is None:
        return "–"
    return f"{v:,.{digits}f}" if digits else f"{round(v):,}"


def tables(data: dict) -> str:
    reps = data["reps"]
    runs = data["runs"]
    out = [
        f"반복 {reps}회, 단계마다 {data['duration_s']:g}초, 동시 {data['concurrency']}, "
        f"행 {data['rows']:,}. 값은 중앙값(최소–최대). 차이는 반복별 짝 비교 (권장 − 기본)/기본 %.",
        "",
    ]
    for workers in data["workers"]:
        cma = f"w{workers}"
        out += [
            f"### uvicorn 워커 {workers}",
            "",
            "| 단계 | 설정 | req/s | p50 ms | p99 ms | 500 | 그중 locked |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for phase in data["phases"]:
            for config in data["configs"]:
                sel = [r for r in runs if r["workers"] == workers and r["config"] == config]
                cells = []
                for name, digits in (
                    ("rps", 1),
                    ("p50_ms", 1),
                    ("p99_ms", 1),
                    ("errors", 0),
                    ("locked", 0),
                ):
                    vals = [v for r in sel if (v := metric(r, phase, name)) is not None]
                    if not vals:
                        cells.append("–")
                        continue
                    med = statistics.median(vals)
                    cells.append(
                        f"{fmt(med, digits)} ({fmt(min(vals), digits)}–{fmt(max(vals), digits)})"
                    )
                out.append(f"| `{phase}` | {LABEL[config]} | " + " | ".join(cells) + " |")
        out += [
            "",
            "| 단계 | req/s 차이 % | p50 차이 % | p99 차이 % | 재현 req/s·p50·p99 | locked 판정 |",
            "|---|---|---|---|---|---|",
        ]
        for phase in data["phases"]:
            cols = []
            for name in PAIRED:
                d = paired_deltas(runs, cma, "recommended", phase, name)
                cols.append((", ".join(f"{x:+.1f}" for x in d), reproduced(d, reps)))
            locked = {
                config: [
                    int(metric(r, phase, "locked") or 0)
                    for r in sorted(runs, key=lambda r: r["rep"])
                    if r["workers"] == workers and r["config"] == config
                ]
                for config in data["configs"]
            }
            verdict = locked_verdict(locked["django-default"], locked["recommended"], reps)
            judged = "·".join("예" if ok else "아니오" for _, ok in cols)
            out.append(
                f"| `{phase}` | " + " | ".join(text for text, _ in cols) + f" | {judged} | "
                f"{verdict} (기본 {locked['django-default']}, 권장 {locked['recommended']}) |"
            )
        out.append("")
    other = {}
    for r in runs:
        for phase in data["phases"]:
            for line, n in (r["phases"][phase].get("exceptions") or {}).items():
                if "database is locked" not in line:
                    other[line] = other.get(line, 0) + n
    out.append(f"locked 밖의 예외 줄: {other or '없음'}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("json", type=Path)
    p.add_argument("--csv", type=Path, help="실행·단계마다 한 줄인 CSV 를 쓴다")
    args = p.parse_args(argv)
    data = json.loads(args.json.read_text(encoding="utf-8"))
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(csv_rows(data))
    print(tables(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
