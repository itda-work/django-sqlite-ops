"""soak 분석(#33, 순수 함수). ``tests/test_lab.py`` 가 Docker 없이 검사한다.

입력은 ``loadgen.py --report-every`` 가 낸 구간 줄들이다(한 ``CONN_MAX_AGE`` 실행분). 구간 줄의
``probe`` 는 앱 프로세스의 ``/lab/soakprobe`` 표본(``notes/metrics.soak_snapshot``)이다. 표본이
없는 구간(앱이 응답하지 못함)은 시계열에서 뺀다.

여기서 내는 기울기·외삽은 모두 **추정**이다. 한 실행의 구간 표본에 최소제곱 직선을 맞춘 것이고,
연결 하나당 메모리는 RSS 를 열린 DB 본체 fd 수(연결 하나가 본체를 하나 연다고 본다)에 회귀한
기울기다. 결과 문서는 이 값을 '추정'으로 적는다.
"""

from __future__ import annotations

import statistics

# 이 값보다 fd 가 적게 변하면 '평탄'으로 본다(요청 10만 건당 fd 1개 미만).
FLAT_FD_PER_REQUEST = 1e-5
# 뒤 절반의 기울기가 앞 절반의 이 비율 이하면 '멈춤'(회수되거나 상한에 닿음).
PLATEAU_RATIO = 0.1


def nofile_limits(limits_text: str) -> tuple[int, int]:
    """``/proc/<pid>/limits`` 원문에서 'Max open files' 의 (soft, hard)."""
    for line in limits_text.splitlines():
        if line.startswith("Max open files"):
            soft, hard = line.split()[3:5]
            return int(soft), int(hard)
    raise ValueError("no 'Max open files' line")


def ols(xs: list[float], ys: list[float]) -> dict | None:
    """최소제곱 직선 y = a + b·x. 점이 3개 미만이거나 x 가 모두 같으면 None."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy else 1.0
    return {"slope": slope, "intercept": my - slope * mx, "r2": r2, "n": n}


def points(rows: list[dict]) -> list[dict]:
    """표본이 있는 구간만, 분석에 쓰는 값으로 편다."""
    out = []
    for r in rows:
        p = r.get("probe")
        if not p or not p.get("fds"):
            continue
        fds = p["fds"]
        out.append(
            {
                "t_s": r["t_s"],
                "cum_requests": r["cum_requests"],
                "db_requests": p.get("db_requests"),
                "conn_created": p.get("conn_created"),
                "fd_total": fds["total"],
                "fd_db": fds["db"],
                "fd_dbfiles": fds["db"] + fds["wal"] + fds["shm"],
                "fd_socket": fds["socket"],
                "fd_other": fds["other"],
                "scan_ms": fds.get("scan_ms"),
                "rss": p.get("VmRSS"),
                "threads": p.get("Threads"),
                "py_threads": p.get("py_threads"),
                "cgroup_current": (p.get("cgroup") or {}).get("current"),
                "wal_bytes": p.get("wal_bytes"),
                "gc_collections": p.get("gc_collections"),
            }
        )
    return out


def _fit(pts: list[dict], x: str, y: str) -> dict | None:
    pairs = [(p[x], p[y]) for p in pts if p[x] is not None and p[y] is not None]
    return ols([a for a, _ in pairs], [b for _, b in pairs])


def trend(pts: list[dict], x: str, y: str) -> dict:
    """전체·앞 절반·뒤 절반 기울기와 모양(``flat``·``grows``·``plateau``·``slows``).

    - ``flat``: 전체 기울기의 절댓값이 ``FLAT_FD_PER_REQUEST`` 미만(x 가 요청 수일 때의 기준).
    - ``plateau``: 앞 절반은 늘고 뒤 절반 기울기가 앞의 ``PLATEAU_RATIO`` 이하.
    - ``grows``: 뒤 절반 기울기가 앞 절반의 절반 이상(꾸준히 는다).
    - ``slows``: 그 사이.
    """
    half = len(pts) // 2
    whole = _fit(pts, x, y)
    first = _fit(pts[: half + 1], x, y)
    last = _fit(pts[half:], x, y)
    ys = [p[y] for p in pts if p[y] is not None]
    out = {
        "start": ys[0] if ys else None,
        "end": ys[-1] if ys else None,
        "max": max(ys) if ys else None,
        "min": min(ys) if ys else None,
        "slope": whole and whole["slope"],
        "r2": whole and round(whole["r2"], 4),
        "slope_first_half": first and first["slope"],
        "slope_last_half": last and last["slope"],
    }
    if whole is None or first is None or last is None:
        out["shape"] = None
    elif abs(whole["slope"]) < FLAT_FD_PER_REQUEST:
        out["shape"] = "flat"
    elif first["slope"] > 0 and last["slope"] <= PLATEAU_RATIO * first["slope"]:
        out["shape"] = "plateau"
    elif first["slope"] > 0 and last["slope"] >= 0.5 * first["slope"]:
        out["shape"] = "grows"
    else:
        out["shape"] = "slows"
    if ys:
        i = ys.index(out["max"])
        out["t_max_s"] = [p["t_s"] for p in pts if p[y] is not None][i]
    return out


def time_to(limit: float | None, current: float | None, per_second: float | None) -> float | None:
    """현재 값이 초당 ``per_second`` 로 늘 때 ``limit`` 에 닿기까지 초(외삽, 추정).

    늘지 않으면 None.
    """
    if limit is None or current is None or not per_second or per_second <= 0:
        return None
    return max(0.0, (limit - current) / per_second)


def analyze(rows: list[dict], *, fd_limit: int | None, mem_limit: int | None) -> dict:
    """한 실행(한 ``CONN_MAX_AGE``)의 요약."""
    pts = points(rows)
    measured = [r for r in rows if r["n"] + r["errors"] > 0]
    errors: dict[str, int] = {}
    for r in rows:
        for k, v in r.get("error_kinds", {}).items():
            errors[k] = errors.get(k, 0) + v
    p99s = [r["p99_ms"] for r in measured if r["n"]]
    out: dict = {
        "intervals": len(rows),
        "intervals_with_probe": len(pts),
        "elapsed_s": rows[-1]["t_s"] if rows else None,
        "requests": rows[-1]["cum_requests"] if rows else 0,
        "errors": rows[-1]["cum_errors"] if rows else 0,
        "error_kinds": errors,
        "intervals_with_errors": sum(1 for r in rows if r["errors"]),
        "rps_mean": round(statistics.fmean(r["rps"] for r in measured), 1) if measured else None,
        "rps_first": measured[0]["rps"] if measured else None,
        "rps_last": measured[-1]["rps"] if measured else None,
        "p99_ms_median": round(statistics.median(p99s), 2) if p99s else None,
        "p99_ms_max": max(p99s) if p99s else None,
        "p99_ms_last": p99s[-1] if p99s else None,
    }
    if not pts:
        return out
    # fd·RSS 를 누적 DB 요청 수에 대해(요청에 비례하는가), 그리고 시간에 대해(외삽).
    out["fd_dbfiles_vs_db_requests"] = trend(pts, "db_requests", "fd_dbfiles")
    out["fd_total_vs_db_requests"] = trend(pts, "db_requests", "fd_total")
    out["rss_vs_db_requests"] = trend(pts, "db_requests", "rss")
    out["threads"] = {
        "os_max": max(p["threads"] or 0 for p in pts),
        "os_end": pts[-1]["threads"],
        "py_max": max(p["py_threads"] or 0 for p in pts),
    }
    out["fd_end"] = {k: pts[-1][k] for k in ("fd_total", "fd_db", "fd_dbfiles", "fd_socket")}
    out["fd_scan_ms_max"] = max(p["scan_ms"] or 0 for p in pts)
    out["rss_end"] = pts[-1]["rss"]
    out["rss_max"] = max(p["rss"] or 0 for p in pts)
    out["cgroup_current_max"] = max(p["cgroup_current"] or 0 for p in pts)
    out["wal_bytes_max"] = max(p["wal_bytes"] or 0 for p in pts)
    out["wal_bytes_end"] = pts[-1]["wal_bytes"]
    if pts[0]["gc_collections"] and pts[-1]["gc_collections"]:
        out["gc_collections_delta"] = [
            b - a for a, b in zip(pts[0]["gc_collections"], pts[-1]["gc_collections"], strict=True)
        ]
    db_req = [p["db_requests"] for p in pts if p["db_requests"] is not None]
    conn = [p["conn_created"] for p in pts if p["conn_created"] is not None]
    if len(db_req) > 1 and len(conn) > 1 and db_req[-1] > db_req[0]:
        out["conn_per_db_request"] = round((conn[-1] - conn[0]) / (db_req[-1] - db_req[0]), 4)

    # 추정: 연결 하나당 메모리 = RSS 를 DB 본체 fd 수에 회귀한 기울기.
    # fd 가 거의 변하지 않으면(50 미만) 내지 않는다.
    fit = _fit(pts, "fd_db", "rss")
    fd_span = max(p["fd_db"] for p in pts) - min(p["fd_db"] for p in pts)
    out["rss_per_connection_est"] = (
        {"bytes": round(fit["slope"]), "r2": round(fit["r2"], 4), "fd_db_span": fd_span}
        if fit and fd_span >= 50
        else None
    )

    # 추정: 뒤 절반의 시간 기울기로 한도까지 남은 시간.
    half = pts[len(pts) // 2 :]
    fd_rate = _fit(half, "t_s", "fd_total")
    rss_rate = _fit(half, "t_s", "rss")
    out["fd_per_s_last_half"] = fd_rate and round(fd_rate["slope"], 3)
    out["rss_per_s_last_half"] = rss_rate and round(rss_rate["slope"], 1)
    out["eta_fd_limit_s_est"] = time_to(fd_limit, pts[-1]["fd_total"], fd_rate and fd_rate["slope"])
    out["eta_mem_limit_s_est"] = time_to(mem_limit, pts[-1]["rss"], rss_rate and rss_rate["slope"])
    return out


def compare(by_cma: dict[str, dict]) -> dict:
    """``{cma: analyze(...)}`` 를 지표별로 나란히 놓는다."""
    keys = (
        "elapsed_s",
        "requests",
        "errors",
        "rps_mean",
        "p99_ms_median",
        "p99_ms_max",
        "rss_end",
        "rss_max",
        "fd_end",
        "wal_bytes_max",
        "conn_per_db_request",
        "rss_per_connection_est",
        "eta_fd_limit_s_est",
        "eta_mem_limit_s_est",
    )
    out = {k: {cma: s.get(k) for cma, s in by_cma.items()} for k in keys}
    out["fd_dbfiles_shape"] = {
        cma: (s.get("fd_dbfiles_vs_db_requests") or {}).get("shape") for cma, s in by_cma.items()
    }
    out["rss_shape"] = {
        cma: (s.get("rss_vs_db_requests") or {}).get("shape") for cma, s in by_cma.items()
    }
    return out
