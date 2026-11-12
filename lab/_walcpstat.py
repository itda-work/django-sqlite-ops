"""#106 집계(순수 함수). Docker 없이 ``tests/test_lab.py`` 가 검사한다.

- id 구간 대조: 앱이 커밋에 성공한 id 구간과 복원본의 id 구간에서 빠진 것·더 있는 것.
- WAL 표본 요약: ``-wal`` 크기, 헤더 salt 가 바뀐 표본 간격 수, ``-shm`` 의 ``mxFrame`` 이 줄어든
  간격 수(WAL 을 처음부터 다시 쓰기 시작함), ``nBackfill`` 이 는 간격 수(체크포인트가 프레임을 DB 로
  옮김), ``nBackfillAttempted > nBackfill`` 인 표본 수(읽는 쪽 때문에 끝까지 못 옮김).
  모두 표본 간격(기본 0.1초) 해상도의 **하한**이다. 한 간격 안의 여러 번은 한 번으로 보인다.
- 정지 구간 요약(Q3): 구간 안 WAL 증가율(MB/분)과 첫·끝 지연 구간.
"""

from __future__ import annotations

Range = list[int]  # [처음, 끝] (끝 포함)


def merge(ranges: list[Range]) -> list[Range]:
    out: list[Range] = []
    for lo, hi in sorted([list(r) for r in ranges]):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def subtract(a: list[Range], b: list[Range]) -> list[Range]:
    """구간 집합 a − b."""
    out: list[Range] = []
    b = merge(b)
    for lo, hi in merge(a):
        cur = lo
        for blo, bhi in b:
            if bhi < cur or blo > hi:
                continue
            if blo > cur:
                out.append([cur, blo - 1])
            cur = max(cur, bhi + 1)
            if cur > hi:
                break
        if cur <= hi:
            out.append([cur, hi])
    return out


def count(ranges: list[Range]) -> int:
    return sum(hi - lo + 1 for lo, hi in ranges)


def compare_ids(committed: list[Range], restored: list[Range]) -> dict:
    """커밋된 id 와 복원본 id 를 대조한다. ``missing`` 이 비어 있지 않으면 커밋이 빠진 것이다.

    ``extra`` 는 복원본에만 있는 id 다(응답을 못 받았지만 커밋된 쓰기 등, 실패가 아니다).
    빠진 것은 둘로 나눈다. ``hole`` 은 복원본의 최대 id 아래에서 빠진 것(그 뒤 커밋은 복제됐는데
    앞 커밋이 없다 — #1540 의 꼴), ``tail`` 은 최대 id 뒤(아직 올라가지 않은 복제 지연일 수 있다).
    """
    missing = subtract(committed, restored)
    extra = subtract(restored, committed)
    top = max((hi for _, hi in restored), default=0)
    holes = [[lo, min(hi, top)] for lo, hi in missing if lo <= top]
    return {
        "committed_rows": count(merge(committed)),
        "restored_rows": count(merge(restored)),
        "missing_rows": count(missing),
        "hole_rows": count(holes),
        "tail_rows": count(missing) - count(holes),
        "missing_ranges": missing[:50],
        "extra_rows": count(extra),
        "extra_ranges": extra[:50],
    }


def _in(s: dict, t_from: float | None, t_to: float | None) -> bool:
    return (t_from is None or s["t"] >= t_from) and (t_to is None or s["t"] <= t_to)


def wal_summary(
    samples: list[dict], t_from: float | None = None, t_to: float | None = None
) -> dict:
    """표본(``walcp_load.py`` 의 ``samples``) 요약. 값이 없는 표본은 그 항목에서 건너뛴다."""
    ss = [s for s in samples if _in(s, t_from, t_to)]
    wal = [s["wal"] for s in ss if s.get("wal") is not None]
    pairs = list(zip(ss, ss[1:], strict=False))
    salt_changes = sum(
        1 for a, b in pairs if a.get("salt") and b.get("salt") and a["salt"] != b["salt"]
    )
    mx_drops = sum(
        1
        for a, b in pairs
        if a.get("mx") is not None and b.get("mx") is not None and b["mx"] < a["mx"]
    )
    backfill_steps = sum(
        1
        for a, b in pairs
        if a.get("nb") is not None and b.get("nb") is not None and b["nb"] > a["nb"]
    )
    blocked = sum(
        1 for s in ss if s.get("nb") is not None and s.get("nba") is not None and s["nba"] > s["nb"]
    )
    mx = [s["mx"] for s in ss if s.get("mx") is not None]
    return {
        "samples": len(ss),
        "wal_max_bytes": max(wal) if wal else None,
        "wal_end_bytes": wal[-1] if wal else None,
        "wal_missing_samples": sum(1 for s in ss if s.get("wal") is None),
        "mx_max_frames": max(mx) if mx else None,
        "salt_changes": salt_changes,
        "mx_drops": mx_drops,
        "backfill_steps": backfill_steps,
        "blocked_samples": blocked,
    }


def growth_mb_per_min(samples: list[dict], t_from: float, t_to: float) -> float | None:
    """구간 안 ``-wal`` 크기의 최소제곱 기울기(MB/분). 표본이 둘 미만이면 None."""
    pts = [(s["t"], s["wal"]) for s in samples if _in(s, t_from, t_to) and s.get("wal") is not None]
    if len(pts) < 2:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mw = sum(w for _, w in pts) / n
    var = sum((t - mt) ** 2 for t, _ in pts)
    if var == 0:
        return None
    slope = sum((t - mt) * (w - mw) for t, w in pts) / var  # 바이트/초
    return round(slope * 60 / 1e6, 2)


def window_latency(intervals: list[dict], t_from: float, t_to: float, width: float) -> dict:
    """구간 [t_from, t_to] 에 완전히 들어가는 지연 구간 가운데 첫·끝 것."""
    inside = [r for r in intervals if r["t"] >= t_from and r["t"] + width <= t_to]
    if not inside:
        return {"first": None, "last": None}
    return {"first": inside[0], "last": inside[-1]}
