"""README 차트(SVG)를 커밋된 원자료 CSV 에서 다시 만든다(#83).

    uv run --no-project --with matplotlib==3.11.2 python docs/assets/charts.py

읽는 것(``docs/research/data/``):
- ``dbconfig-2026-10-26.csv`` — Django 기본 대 권장 설정(``lab/dbconfig_report.py --csv`` 출력)
- ``conn-max-age-2026-10-08.csv`` — ``CONN_MAX_AGE`` 0 대 None 의 열린 DB fd·500
  (#26 원자료에서 뽑음)

쓰는 것(``docs/assets/``): ``dbconfig.svg``, ``conn-max-age.svg``.

``conn-max-age-2026-10-08.csv`` 는 git 무시인 #26 원자료에서 한 번 뽑았다(그 원자료가 있는 곳에서만
다시 뽑을 수 있다):

    uv run --no-project python docs/assets/charts.py extract-cma \\
        lab/.out/bench-20261008-234839.json

글자는 matplotlib 에 들어 있는 DejaVu Sans 로만 쓴다(한글 글꼴이 없는 머신에서도 같은 SVG 가 나오게
영어로 적는다). 배경은 흰 카드(투명 아님)라 라이트·다크 화면 모두에서 읽힌다. 두 계열 색은
Okabe–Ito 계열의 주황·파랑으로 색각 이상 검사(dataviz 검증기, 인접 ΔE 24.7)를 통과했고, 계열
이름을 막대 옆에 직접 적어 색에만 기대지 않는다.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "research" / "data"
DBCONFIG_CSV = DATA / "dbconfig-2026-10-26.csv"
CMA_CSV = DATA / "conn-max-age-2026-10-08.csv"

INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
CARD = "#ffffff"
DEFAULT = "#eb6834"  # Django 기본 / CONN_MAX_AGE=None (문제가 있는 쪽)
RECOMMENDED = "#2a78d6"  # 권장 / CONN_MAX_AGE=0
PHASE_LABEL = {
    "mixed": "mixed\n70% read · 10% sort\n20% 1-row insert",
    "write": "write\n30% read\n70% 50-row insert",
    "rmw": "rmw\n50% read\n50% read-modify-write",
}


def setup():
    import matplotlib

    matplotlib.use("svg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "svg.fonttype": "path",
            "svg.hashsalt": "django-sqlite-ops",
            "axes.edgecolor": INK2,
            "axes.labelcolor": INK,
            "xtick.color": INK2,
            "ytick.color": INK2,
            "text.color": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "axes.axisbelow": True,
        }
    )
    return plt


def save(fig, name: str) -> None:
    fig.savefig(HERE / name, format="svg", facecolor=CARD, metadata={"Date": None})


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def grouped(ax, groups, series, values, *, fmt="{:,.0f}"):
    """``values[(group, series)]`` = 반복 값 목록. 막대는 중앙값, 점은 반복 하나씩."""
    width = 0.36
    for gi, group in enumerate(groups):
        for si, (name, color, _) in enumerate(series):
            vals = values[(group, name)]
            x = gi + (si - 0.5) * width
            med = statistics.median(vals)
            ax.bar(x, med, width * 0.92, color=color, edgecolor=CARD, linewidth=1.5, zorder=2)
            ax.scatter([x] * len(vals), vals, s=9, color=INK, alpha=0.55, zorder=3, linewidths=0)
            ax.annotate(
                fmt.format(med),
                (x, max(vals)),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8,
                color=INK,
            )
    ax.set_xticks(range(len(groups)))


def dbconfig_chart(plt) -> None:
    rows = read_csv(DBCONFIG_CSV)
    phases = list(dict.fromkeys(r["phase"] for r in rows))
    workers = sorted({int(r["workers"]) for r in rows})
    reps = len({r["rep"] for r in rows})
    series = [
        ("django-default", DEFAULT, "Django default (rollback journal, DEFERRED)"),
        ("recommended", RECOMMENDED, "sqlite_database() (WAL, IMMEDIATE, NORMAL)"),
    ]
    metrics = [
        ("rps", "Throughput (req/s)", "{:,.0f}"),
        ("p99_ms", "p99 latency (ms)", "{:,.0f}"),
        ("locked", '"database is locked"\n500s per 20 s run', "{:,.0f}"),
    ]
    fig, axes = plt.subplots(
        len(metrics), len(workers), figsize=(9.6, 9.2), sharey="row", squeeze=False
    )
    for wi, w in enumerate(workers):
        for mi, (key, label, fmt) in enumerate(metrics):
            ax = axes[mi][wi]
            values = {
                (p, s): [
                    float(r[key])
                    for r in rows
                    if r["phase"] == p and r["config"] == s and int(r["workers"]) == w
                ]
                for p in phases
                for s, _, _ in series
            }
            grouped(ax, phases, series, values, fmt=fmt)
            last = mi == len(metrics) - 1
            ax.set_xticklabels([PHASE_LABEL[p] if last else p for p in phases], fontsize=8)
            if wi == 0:
                ax.set_ylabel(label, fontsize=9)
            if mi == 0:
                ax.set_title(f"uvicorn --workers {w}", fontsize=10, color=INK, loc="left", pad=14)
            ax.margins(y=0.15)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, c, _ in series]
    fig.legend(
        handles,
        [t for _, _, t in series],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.965),
        ncol=2,
        frameon=False,
        fontsize=9,
    )
    fig.suptitle(
        "Django default SQLite vs django-sqlite-ops recommended settings",
        x=0.02,
        ha="left",
        fontsize=12,
        fontweight="bold",
    )
    fig.text(
        0.02,
        0.005,
        f"Bars: median of {reps} alternating runs per setting; dots: each run. Throughput and "
        "latency count successful requests only.\n16 concurrent clients, Django 6.1.2 + uvicorn, "
        "SQLite 3.46.1, Docker (colima, arm64), no Litestream. Measured 2026-10-26 (#83).",
        fontsize=7.5,
        color=INK2,
    )
    fig.tight_layout(rect=(0, 0.02, 1, 0.94))
    save(fig, "dbconfig.svg")


def cma_chart(plt) -> None:
    rows = read_csv(CMA_CSV)
    order = [("0", RECOMMENDED, "CONN_MAX_AGE=0"), ("none", DEFAULT, "CONN_MAX_AGE=None")]
    limit = {int(r["fd_limit"]) for r in rows}
    n = {c: sum(1 for r in rows if r["cma"] == c) for c, _, _ in order}
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 3.9), gridspec_kw={"width_ratios": [1.6, 1]})
    for i, (cma, color, _) in enumerate(order):
        fds = [int(r["db_fds_max"]) for r in rows if r["cma"] == cma]
        # 겹침을 줄이려고 반복·단계 순서대로 가로로 조금씩 벌린다(결정적)
        xs = [i + (k / max(1, len(fds) - 1) - 0.5) * 0.5 for k in range(len(fds))]
        ax1.scatter(xs, fds, s=14, color=color, edgecolor=CARD, linewidths=0.6, zorder=3)
        ax1.annotate(
            f"max {max(fds):,}",
            (i - 0.3, max(fds)),
            xytext=(-6, 0),
            textcoords="offset points",
            ha="right",
            va="center",
            fontsize=8,
        )
        errors = sum(int(r["errors"]) for r in rows if r["cma"] == cma)
        ax2.bar(i, errors, 0.6, color=color, edgecolor=CARD, linewidth=1.5, zorder=2)
        ax2.annotate(
            f"{errors:,}",
            (i, errors),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=9,
        )
    for lim in limit:
        ax1.axhline(lim, color=INK2, linestyle="--", linewidth=1)
        ax1.annotate(
            f"open-files soft limit {lim:,}",
            (-0.55, lim),
            xytext=(0, 4),
            textcoords="offset points",
            ha="left",
            fontsize=8,
            color=INK2,
        )
    labels = [f"{label.removeprefix('CONN_MAX_AGE=')}\n(n={n[c]})" for c, _, label in order]
    ax1.set_xticks([0, 1], labels, fontsize=9)
    ax1.set_xlabel("CONN_MAX_AGE", fontsize=9)
    ax1.set_xlim(-0.6, 1.6)
    ax1.set_ylim(0, max(limit) * 1.15)
    ax1.set_ylabel("Max open DB file descriptors\nper 20 s run", fontsize=9)
    ax2.set_xticks([0, 1], labels, fontsize=9)
    ax2.set_xlabel("CONN_MAX_AGE", fontsize=9)
    ax2.set_ylabel('HTTP 500 "unable to open\ndatabase file", total', fontsize=9)
    ax2.margins(y=0.2)
    fig.suptitle(
        "ASGI + sync views: CONN_MAX_AGE=None never reuses connections, it piles them up",
        x=0.02,
        ha="left",
        fontsize=11,
        fontweight="bold",
    )
    fig.text(
        0.02,
        0.01,
        "n = 20 s bench runs (5 reps x 4 PRAGMA variants x 2 load phases). 16 concurrent "
        "clients, uvicorn 1 worker, Django 6.1.2.\nLab bench of 2026-10-08 (#26). "
        "The system check sqlite_ops.W005 flags CONN_MAX_AGE=None under ASGI.",
        fontsize=7.5,
        color=INK2,
    )
    fig.tight_layout(rect=(0, 0.07, 1, 0.93))
    save(fig, "conn-max-age.svg")


def extract_cma(bench_json: Path) -> None:
    """#26 벤치 원자료에서 단계마다 열린 DB fd 최대·500 수를 뽑는다."""
    data = json.loads(bench_json.read_text(encoding="utf-8"))
    rows = []
    for run in data["runs"]:
        for phase, res in run["phases"].items():
            rows.append(
                {
                    "source": bench_json.name,
                    "cma": run["cma"],
                    "variant": run["variant"],
                    "rep": run["rep"],
                    "phase": phase,
                    "concurrency": res["concurrency"],
                    "db_fds_max": res["server"]["db_fds_max"],
                    "fd_limit": res["server"]["fd_limit"],
                    "errors": res["errors"],
                }
            )
    rows.sort(key=lambda r: (r["cma"], r["rep"], r["variant"], r["phase"]))
    with CMA_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str]) -> int:
    if argv[:1] == ["extract-cma"]:
        extract_cma(Path(argv[1]))
        return 0
    plt = setup()
    dbconfig_chart(plt)
    cma_chart(plt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
