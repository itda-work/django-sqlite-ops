"""``cache_probe`` 집계·판정(순수 함수, #27). ``tests/test_lab.py`` 가 Docker 없이 검사한다.

칸(cell) 하나는 ``cache_probe.py`` 가 내는 ``{"kind": "cell", "size", "threads", "mode", "block",
"variant", "ops_per_s", "p99_us", ...}`` 다. 조건은 ``(size, threads, mode)`` 이다.

판정 기준(본 측정 전에 정함, 결과 문서에 적는다):

- 같은 블록의 기준(baseline)과 짝지어 (변형 − 기준) / 기준 × 100 을 낸다(반올림 안 함).
- 1차: 블록 짝 차이의 평균과 t 분포 95% 신뢰구간. 구간이 0 을 포함하지 않고 |평균| ≥ 5% 면
  '재현', 구간 전체가 (−5%, +5%) 안이면 '5% 이상 효과 없음', 그 밖은 '판정 불가'.
- 보조(#26 과 같다): 모든 블록에서 같은 방향으로 5% 이상.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable

THRESHOLD_PCT = 5.0
# t 분포 0.975 분위수(자유도 1..30). 그보다 크면 정규 근사 1.96.
_T975 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip

REPRODUCED_WORSE = "재현(나빠짐)"
REPRODUCED_BETTER = "재현(좋아짐)"
NO_EFFECT = "5% 이상 효과 없음"
UNDECIDED = "판정 불가"


def t975(df: int) -> float:
    if df < 1:
        raise ValueError("df must be >= 1")
    return _T975[df - 1] if df <= len(_T975) else 1.96


def value(cell: dict, name: str) -> float | None:
    """점으로 이은 키 경로(예: ``phases_us.open.p50``)의 숫자, 없으면 ``None``."""
    v: object = cell
    for key in name.split("."):
        if not isinstance(v, dict):
            return None
        v = v.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def condition(cell: dict) -> tuple:
    return (cell["size"], cell["threads"], cell["mode"])


def cells_of(records: Iterable[dict]) -> list[dict]:
    """``kind == "cell"`` 만(워밍업 칸은 버린다)."""
    return [r for r in records if r.get("kind") == "cell"]


def paired_deltas(cells: Iterable[dict], cond: tuple, variant: str, name: str) -> list[float]:
    """블록 순서대로 (변형 − 기준) / 기준 × 100. 기준이 없거나 0 인 블록, 값이 없는 블록은 뺀다."""
    cells = [c for c in cells if condition(c) == cond]
    base = {c["block"]: value(c, name) for c in cells if c["variant"] == "baseline"}
    out = []
    for c in sorted(cells, key=lambda c: c["block"]):
        if c["variant"] != variant:
            continue
        v, b = value(c, name), base.get(c["block"])
        if v is None or not b:
            continue
        out.append(100 * (v - b) / b)
    return out


def mean_ci(deltas: list[float]) -> tuple[float, float, float] | None:
    """(평균, 95% 하한, 95% 상한). 짝이 2개 미만이면 ``None``."""
    if len(deltas) < 2:
        return None
    m = statistics.fmean(deltas)
    half = t975(len(deltas) - 1) * statistics.stdev(deltas) / math.sqrt(len(deltas))
    return m, m - half, m + half


def verdict(
    deltas: list[float], *, higher_is_better: bool = True, threshold: float = THRESHOLD_PCT
) -> str:
    """1차 판정. 방향은 지표에 따라 다르다(처리량은 클수록, 지연은 작을수록 좋다)."""
    ci = mean_ci(deltas)
    if ci is None:
        return UNDECIDED
    m, lo, hi = ci
    if (lo > 0 or hi < 0) and abs(m) >= threshold:
        return REPRODUCED_BETTER if (m > 0) == higher_is_better else REPRODUCED_WORSE
    if lo > -threshold and hi < threshold:
        return NO_EFFECT
    return UNDECIDED


def same_direction(deltas: list[float], expected: int, threshold: float = THRESHOLD_PCT) -> bool:
    """보조 판정(#26): 블록 수만큼 짝이 있고 모두 같은 방향으로 ``threshold``% 이상."""
    if not deltas or len(deltas) != expected:
        return False
    return all(d >= threshold for d in deltas) or all(d <= -threshold for d in deltas)


def median_of(cells: Iterable[dict], cond: tuple, variant: str, name: str) -> float | None:
    vals = [
        v
        for c in cells
        if condition(c) == cond and c["variant"] == variant and (v := value(c, name)) is not None
    ]
    return statistics.median(vals) if vals else None


def max_of(cells: Iterable[dict], cond: tuple, variant: str, name: str) -> float | None:
    vals = [
        v
        for c in cells
        if condition(c) == cond and c["variant"] == variant and (v := value(c, name)) is not None
    ]
    return max(vals) if vals else None


def compare(
    cells: list[dict],
    cond: tuple,
    variant: str,
    name: str,
    *,
    blocks: int,
    higher_is_better: bool = True,
) -> dict:
    """한 조건·한 변형·한 지표의 짝 차이 요약."""
    deltas = paired_deltas(cells, cond, variant, name)
    ci = mean_ci(deltas)
    return {
        "n": len(deltas),
        "mean": ci[0] if ci else None,
        "lo": ci[1] if ci else None,
        "hi": ci[2] if ci else None,
        "min": min(deltas) if deltas else None,
        "max": max(deltas) if deltas else None,
        "verdict": verdict(deltas, higher_is_better=higher_is_better),
        "same_direction": same_direction(deltas, blocks),
    }


# --- 랩 벤치 원자료의 요청 종류별 지연(#10·#26 원자료 재분석) -----------------------------------

BENCH_KINDS = ("read", "sorted", "write")


def bench_kinds(run: dict, phase: str = "mixed") -> dict | None:
    """벤치 실행 하나의 요청 종류별 평균 지연(ms).

    #26 형식은 ``svc``(서버 쪽)를, #10 형식은 클라이언트 평균을 쓴다.
    ``{"source": "svc"|"client", "read": ms, "sorted": ms, "write": ms}``.
    """
    kinds = (
        run.get("phases", {}).get(phase, {}).get("kinds") if "phases" in run else run.get("kinds")
    )
    if not isinstance(kinds, dict) or not all(k in kinds for k in BENCH_KINDS):
        return None
    if all(isinstance(kinds[k].get("svc"), dict) for k in BENCH_KINDS):
        return {"source": "svc", **{k: kinds[k]["svc"]["mean_ms"] for k in BENCH_KINDS}}
    return {"source": "client", **{k: kinds[k]["mean_ms"] for k in BENCH_KINDS}}


def bench_kind_rows(data: dict, variants: Iterable[str], cma: str = "0") -> list[dict]:
    """반복마다 같은 반복의 기준과 짝지어 종류별 지연 차이(%)와 정렬/읽기 비를 낸다.

    #10 형식(실행에 ``cma`` 가 없음)은 ``CONN_MAX_AGE=0`` 하나뿐이라 ``cma`` 를 보지 않는다.
    """
    runs = [r for r in data["runs"] if r.get("cma", cma) == cma]
    base = {r["rep"]: bench_kinds(r) for r in runs if r["variant"] == "baseline"}
    out = []
    for variant in variants:
        for r in sorted((r for r in runs if r["variant"] == variant), key=lambda r: r["rep"]):
            k, b = bench_kinds(r), base.get(r["rep"])
            if k is None or b is None:
                continue
            out.append(
                {
                    "variant": variant,
                    "rep": r["rep"],
                    "source": k["source"],
                    **{f"d_{x}": 100 * (k[x] - b[x]) / b[x] for x in BENCH_KINDS},
                    "ratio": k["sorted"] / k["read"],
                    "base_ratio": b["sorted"] / b["read"],
                }
            )
    return out
