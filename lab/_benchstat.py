"""PRAGMA 벤치 집계·판정(순수 함수). ``tests/test_lab.py`` 가 Docker 없이 검사한다.

실행 기록 하나(run)는 ``{"cma", "variant", "rep", "phases": {phase: loadgen 결과}}`` 다. 지표 이름은
loadgen 결과의 키 경로를 점으로 잇는다(예: ``svc.p50_ms``, ``server.conn_per_db_request``).

판정(#10 과 같다): 같은 반복·같은 ``CONN_MAX_AGE``·같은 단계의 기준(baseline)과 짝지어 차이(%)를
내고, 모든 반복에서 같은 방향으로 ``threshold``% 이상일 때만 '재현'이다.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable

THRESHOLD_PCT = 5.0


def metric(run: dict, phase: str, name: str) -> float | None:
    value: object = run.get("phases", {}).get(phase)
    for key in name.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def paired_deltas(runs: Iterable[dict], cma: str, variant: str, phase: str, name: str) -> list:
    """반복마다 (변형 − 기준) / 기준 × 100. 기준이 0 이거나 값이 없는 반복은 뺀다."""
    runs = list(runs)
    base = {
        r["rep"]: metric(r, phase, name)
        for r in runs
        if r["cma"] == cma and r["variant"] == "baseline"
    }
    out = []
    for r in sorted(runs, key=lambda r: r["rep"]):
        if r["cma"] != cma or r["variant"] != variant:
            continue
        v, b = metric(r, phase, name), base.get(r["rep"])
        if v is None or not b:
            continue
        out.append(round(100 * (v - b) / b, 1))
    return out


def reproduced(deltas: list, expected: int, threshold: float = THRESHOLD_PCT) -> bool:
    """반복 수만큼 짝이 있고, 모두 같은 방향으로 ``threshold``% 이상."""
    if not deltas or len(deltas) != expected:
        return False
    return all(d >= threshold for d in deltas) or all(d <= -threshold for d in deltas)


def describe(values: list) -> dict:
    return {
        "mean": round(statistics.fmean(values), 3) if values else None,
        "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "values": values,
    }


def summarize(
    runs: list[dict],
    *,
    cmas: Iterable[str],
    variants: Iterable[str],
    phases: Iterable[str],
    metrics: Iterable[str],
    paired: Iterable[str],
    reps: int,
) -> dict:
    """``{cma: {phase: {variant: {metric: {...}}}}}``.

    ``paired`` 지표에만 짝 차이와 판정을 붙인다.
    """
    paired = set(paired)
    out: dict = {}
    for cma in cmas:
        out[cma] = {}
        for phase in phases:
            out[cma][phase] = {}
            for variant in variants:
                rows = [r for r in runs if r["cma"] == cma and r["variant"] == variant]
                rows.sort(key=lambda r: r["rep"])
                entry: dict = {}
                for name in metrics:
                    vals = [v for r in rows if (v := metric(r, phase, name)) is not None]
                    entry[name] = describe(vals)
                    if variant != "baseline" and name in paired:
                        deltas = paired_deltas(runs, cma, variant, phase, name)
                        entry[name]["delta_pct_per_rep"] = deltas
                        entry[name]["reproduced"] = reproduced(deltas, reps)
                entry["errors"] = sum(int(metric(r, phase, "errors") or 0) for r in rows)
                out[cma][phase][variant] = entry
    return out
