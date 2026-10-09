"""soak 원자료(``lab/.out/soak-<run>.jsonl``)에서 결과 문서의 표와 수치를 만든다(#33 리뷰 1).

    uv run --no-project python lab/soak_report.py lab/.out/soak-<run>.jsonl > 표.md

결과 문서의 수치는 손으로 옮기지 않고 이 출력에서 가져온다. 계산은 ``_soakstat`` 의 같은 함수
(``analyze``·``describe``·``quantile_nearest``)를 쓴다. 한 파일의 한 실행만 읽는다(옛 실행과 섞지
않는다). 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _soakstat import analyze, nofile_limits  # noqa: E402

MIB = 2**20
SERIES_T = (10, 30, 120, 300, 600, 900, 1200, 1500, 1800)


def load(path: Path) -> dict:
    runs: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        run = runs.setdefault(r["cma"], {"rows": []})
        if r["kind"] == "interval":
            run["rows"].append(r)
        else:
            run[r["kind"]] = r
    return runs


def n(v, digits=0) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and digits:
        return f"{v:,.{digits}f}"
    return f"{round(v):,}"


def mib(v, digits=0) -> str:
    return "–" if v is None else n(v / MIB, digits) if digits else f"{round(v / MIB):,}"


def series(cma: str, run: dict) -> list[str]:
    out = [
        f"`CONN_MAX_AGE={'None' if cma == 'none' else cma}`",
        "",
        "| t (s) | 누적 요청 | req/s | p99 ms | 오류 | fd 합계 | DB 본체 | `-wal` | `-shm` "
        "| 소켓 | RSS MiB | OS 스레드 | cgroup MiB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def line(label, r, p):
        f = p.get("fds") or {}
        cg = (p.get("cgroup") or {}).get("current")
        req = "–" if r is None else n(r["cum_requests"])
        rps = "–" if r is None or r.get("tail") else n(r["rps"], 1)
        p99 = "–" if r is None or r.get("tail") else n(r["p99_ms"], 1)
        err = "–" if r is None else n(r["errors"])
        return (
            f"| {label} | {req} | {rps} | {p99} | {err} | {n(f.get('total'))} | {n(f.get('db'))} "
            f"| {n(f.get('wal'))} | {n(f.get('shm'))} | {n(f.get('socket'))} "
            f"| {mib(p.get('VmRSS'))} | {n(p.get('Threads'))} | {mib(cg)} |"
        )

    out.append(line("직전", None, run["baseline"]["probe"]))
    by_t = {round(r["t_s"]): r for r in run["rows"] if not r.get("tail")}
    for t in SERIES_T:
        if t in by_t and by_t[t].get("probe"):
            out.append(line(f"{t:,}", by_t[t], by_t[t]["probe"]))
    tail = [r for r in run["rows"] if r.get("tail")]
    for r in tail:
        out.append(line(f"꼬리 {r['t_s']:,.1f}", r, r.get("probe") or {}))
    return out


def dist_row(label: str, a: dict, keys: tuple, fmt) -> str:
    cells = []
    for s in a:
        d = s["dist"][keys]
        cells.append(" / ".join(fmt(d[k]) for k in ("min", "median", "p90", "max")))
    return f"| {label} | " + " | ".join(cells) + " |"


def main(path: Path) -> None:
    runs = load(path)
    cmas = list(runs)
    res = {}
    for cma, run in runs.items():
        limits = nofile_limits(run["env"]["self_limits"])
        res[cma] = analyze(
            run["rows"],
            fd_limit=limits[0],
            mem_limit=run["env"]["cgroup"]["max"],
            baseline=run["baseline"]["probe"],
        )
    print(f"<!-- 생성: uv run --no-project python lab/soak_report.py {path} -->\n")
    print("## 환경(원자료 kind=env)\n")
    for cma, run in runs.items():
        e = run["env"]
        fin = run.get("final", {})
        print(
            f"- {cma}: pid {e['pid']}, self nofile {nofile_limits(e['self_limits'])}, "
            f"pid1 nofile {nofile_limits(e['pid1_limits'])}, nr_open {e['nr_open'].strip()}, "
            f"file-max {e['file_max'].strip()}, cgroup {e['cgroup']}, "
            f"conn_max_age {e['conn_max_age']}, pid1 `{e['pid1_cmdline'][:60]}…`"
        )
        print(
            f"  - final: stop_reason {fin.get('stop_reason')}, elapsed {fin.get('elapsed_s')}, "
            f"intervals {fin.get('intervals')}, tail {fin.get('tail_requests')}, "
            f"workers_alive {fin.get('workers_alive')}, cum {fin.get('cum_requests')} / "
            f"ok {fin.get('cum_ok')} / err {fin.get('cum_errors')}"
        )
        s = run.get("summary", {})
        print(
            f"  - summary: harness_problems {s.get('harness_problems')}, loadgen_rc "
            f"{s.get('loadgen_rc')}, app_state {s.get('app_state')}, "
            f"nonjson {len(s.get('loadgen_nonjson_stdout') or [])}"
        )
        last = run["rows"][-1].get("probe") or {}
        print(f"  - 마지막 표본 cgroup: {last.get('cgroup')}")
    print("\n## 시계열(원자료에서 고른 구간)\n")
    for cma in cmas:
        print("\n".join(series(cma, runs[cma])) + "\n")

    a = [res[c] for c in cmas]
    head = "| 지표 | " + " | ".join(f"`{c}`" for c in cmas) + " |"
    sep = "|---|" + "---|" * len(cmas)
    print("## 분포와 기울기\n")
    print("중앙값 = `statistics.median`, p90 = 최근접 순위(`ceil(0.9 n)` 번째). 꼬리 줄 제외.\n")
    print(head)
    print(sep)

    def row(label, fn):
        print(f"| {label} | " + " | ".join(fn(s) for s in a) + " |")

    row("구간 수 / 표본 있는 구간", lambda s: f"{s['intervals']} / {s['intervals_with_probe']}")
    row("요청 / 오류 (꼬리 포함)", lambda s: f"{n(s['requests'])} / **{n(s['errors'])}**")
    row("꼬리 요청 / 꼬리 오류", lambda s: f"{s['tail_requests']} / {s['tail_errors']}")
    row(
        "서버 DB 요청 증가 − 클라이언트 요청",
        lambda s: (
            f"{n(s.get('server_db_requests'))} − {n(s['requests'])} = "
            f"{s.get('server_minus_client_requests')}"
        ),
    )
    row(
        "req/s 평균 (첫 / 끝 구간)",
        lambda s: f"{n(s['rps_mean'], 1)} ({n(s['rps_first'], 1)} / {n(s['rps_last'], 1)})",
    )
    row("구간 p99 중앙값 / 최대 (ms)", lambda s: f"{s['p99_ms_median']} / {s['p99_ms_max']}")
    print(dist_row("fd 합계 최소 / 중앙값 / p90 / 최대", a, "fd_total", n))
    print(dist_row("DB 본체 fd 최소 / 중앙값 / p90 / 최대", a, "fd_db", n))
    print(dist_row("`-wal` fd 최소 / 중앙값 / p90 / 최대", a, "fd_wal", n))
    print(dist_row("앱 RSS MiB 최소 / 중앙값 / p90 / 최대", a, "rss", lambda v: mib(v, 1)))
    print(dist_row("OS 스레드 최소 / 중앙값 / p90 / 최대", a, "threads", n))
    print(dist_row("cgroup MiB 최소 / 중앙값 / p90 / 최대", a, "cgroup_current", mib))
    row("`VmHWM` 끝 (MiB)", lambda s: mib(s["hwm_end"]))
    row(
        f"{a[0]['steady_after_s']:.0f}초 뒤 평균 fd 합계 / DB 본체",
        lambda s: f"{n(s['steady_mean']['fd_total'], 1)} / {n(s['steady_mean']['fd_db'], 1)}",
    )
    row(
        f"{a[0]['steady_after_s']:.0f}초 뒤 평균 RSS / cgroup (MiB)",
        lambda s: (
            f"{mib(s['steady_mean']['rss'], 1)} / {mib(s['steady_mean']['cgroup_current'], 1)}"
        ),
    )
    row("DB 요청당 연결 생성", lambda s: str(s.get("conn_per_db_request")))

    def trend(key):
        def fmt(s):
            t = s[key]
            return (
                f"{t['slope']:.3g} (r² {t['r2']}), 앞 {t['slope_first_half']:.3g} → "
                f"뒤 {t['slope_last_half']:.3g}, `{t['shape']}`"
            )

        return fmt

    row("DB 파일 fd / 누적 DB 요청 기울기", trend("fd_dbfiles_vs_db_requests"))
    row("RSS(B) / 누적 DB 요청 기울기", trend("rss_vs_db_requests"))
    row(
        "뒤 절반 시간 기울기 fd/초, RSS B/초",
        lambda s: f"{s['fd_per_s_last_half']} / {s['rss_per_s_last_half']}",
    )
    row("fd 한도까지 외삽 (초)", lambda s: n(s["eta_fd_limit_s_est"]))
    row("메모리 한도까지 외삽 (초)", lambda s: n(s["eta_mem_limit_s_est"]))
    row(
        "연결당 메모리 회귀 (B/DB 본체 fd)",
        lambda s: (
            "–"
            if not s["rss_per_connection_est"]
            else "{} (r² {}, 범위 {})".format(
                n(s["rss_per_connection_est"]["bytes"]),
                s["rss_per_connection_est"]["r2"],
                s["rss_per_connection_est"]["fd_db_span"],
            )
        ),
    )
    row("gc 수집 횟수 세대 0/1/2", lambda s: " / ".join(map(n, s["gc_collections_delta"])))
    row(
        "fd 열거 `scan_ms` 최대 / 표본 응답 최대 (ms)",
        lambda s: f"{s['dist']['scan_ms']['max']} / {s['probe_ms_max']}",
    )
    row("WAL 최대 / 끝 (B)", lambda s: f"{n(s['wal_bytes_max'])} / {n(s['wal_bytes_end'])}")

    if {"none", "0"} <= set(res) and all(
        res[c].get("steady_mean", {}).get("rss") for c in ("none", "0")
    ):
        sn, s0 = res["none"]["steady_mean"], res["0"]["steady_mean"]
        print("\n## 두 실행 비교(추정 포함)\n")
        print(
            f"- fd 합계 비 {sn['fd_total'] / s0['fd_total']:.2f}, "
            f"RSS 비 {sn['rss'] / s0['rss']:.2f}"
        )
        per = (sn["rss"] - s0["rss"]) / (sn["fd_db"] - s0["fd_db"])
        print(
            f"- 연결당 메모리(평균 차): ({mib(sn['rss'], 1)} − {mib(s0['rss'], 1)} MiB) / "
            f"({n(sn['fd_db'], 1)} − {n(s0['fd_db'], 1)}) = {per / 1024:.1f} KiB"
        )
    print("\n## DB 본체 fd 가 바뀐 구간\n")
    for cma in cmas:
        prev, steps = None, []
        for r in runs[cma]["rows"]:
            d = (r.get("probe") or {}).get("fds", {}).get("db")
            if d is not None and d != prev:
                steps.append(f"{r['t_s']:g}s→{d}" + ("(꼬리)" if r.get("tail") else ""))
                prev = d
        print(f"- {cma}: " + ", ".join(steps[:40]) + (" …" if len(steps) > 40 else ""))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
