"""``cache_probe`` 원자료에서 결과 문서의 표를 만든다(#27).

    uv run --no-project python lab/cache_report.py lab/.out/cache-macos-<시각>.jsonl \\
        lab/.out/cache-linux-<시각>.jsonl > 표.md

    uv run --no-project python lab/cache_report.py --bench lab/.out/bench-<시각>.json ...

결과 문서의 수치는 손으로 옮기지 않고 이 출력에서 가져온다. 계산은 ``_cachestat`` 의 함수를 쓴다.
파일 하나가 실행(환경) 하나다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cachestat import (  # noqa: E402
    bench_kind_rows,
    cells_of,
    compare,
    max_of,
    median_of,
    value,
)

MIB = 2**20
PHASES = ("open", "pragma", "query", "close")


def load(path: Path) -> tuple[dict, list[dict]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    metas = [r for r in records if r.get("kind") == "meta"]
    if len(metas) != 1:
        raise SystemExit(f"{path}: meta 줄이 {len(metas)}개(한 실행만 읽는다)")
    if not any(r.get("kind") == "end" for r in records):
        raise SystemExit(f"{path}: end 줄이 없다(중단된 실행)")
    return metas[0], cells_of(records)


def pct(v: float | None) -> str:
    return "–" if v is None else f"{v:+.1f}"


def num(v: float | None, digits: int = 0) -> str:
    return "–" if v is None else f"{v:,.{digits}f}"


def cond_label(cond: tuple) -> str:
    size, threads, mode = cond
    return f"| {size} | {threads} | {mode} "


def env_table(runs: list[tuple[dict, list[dict]]]) -> list[str]:
    out = [
        "| 환경 | Python | SQLite | 플랫폼 | CPU | cgroup memory.max | small | large "
        "| 블록 × 칸 | 스크립트 sha256 | `sqlite3_status` |",
        "|---|---|---|---|---:|---|---|---|---|---|---|",
    ]
    for meta, _ in runs:
        sizes = meta["sizes"]

        def size(name, sizes=sizes):
            # cache_mech 는 템플릿 하나(행 100,000)라 small 칸에만 적는다.
            s = sizes.get(name) or (sizes.get("template") if name == "small" else None)
            return "–" if s is None else f"{s['rows']:,}행, {s['bytes'] / MIB:.1f} MiB"

        status = "예" if meta["sqlite_status"] else "아니오(RSS 만)"
        out.append(
            f"| {meta['env']} | {meta['python']} | {meta['sqlite']} | {meta['platform']} "
            f"| {meta['cpus']} | {meta['cgroup_memory_max'] or '–'} | {size('small')} "
            f"| {size('large')} | {meta['blocks']} × {meta['cell_s']}초 "
            f"| `{meta['probe_sha256'][:12]}` | {status} |"
        )
    return out


def verdict_table(meta: dict, cells: list[dict]) -> list[str]:
    has_p99 = any(value(c, "p99_us") is not None for c in cells)
    first = "요청" if meta.get("mech") else "크기"
    head = (
        f"| {first} | 스레드 | 패턴 | 변형 | 기준 ops/s(중앙값) | Δ처리량 % 평균 [95% CI] | 범위 "
    )
    head += "| 판정 | 전 블록 같은 방향 ≥5% |"
    rule = "|---|---:|---|---|---:|---|---|---|---|"
    if has_p99:
        head += " Δp99 % 평균 [95% CI] | p99 판정 |"
        rule += "---|---|"
    out = [head, rule]
    blocks = meta["blocks"]
    for cond in map(tuple, meta["conditions"]):
        base = median_of(cells, cond, "baseline", "ops_per_s")
        for variant in meta["variants"][1:]:
            t = compare(cells, cond, variant, "ops_per_s", blocks=blocks)
            line = (
                f"{cond_label(cond)}| `{variant}` | {num(base)} "
                f"| {pct(t['mean'])} [{pct(t['lo'])}, {pct(t['hi'])}] "
                f"| {pct(t['min'])} … {pct(t['max'])} | {t['verdict']} "
                f"| {'예' if t['same_direction'] else '아니오'} |"
            )
            if has_p99:
                p = compare(cells, cond, variant, "p99_us", blocks=blocks, higher_is_better=False)
                line += f" {pct(p['mean'])} [{pct(p['lo'])}, {pct(p['hi'])}] | {p['verdict']} |"
            out.append(line)
    return out


def phase_table(meta: dict, cells: list[dict]) -> list[str]:
    out = [
        "| 크기 | 스레드 | 패턴 | 변형 | open | PRAGMA | 요청(연결당 합) | close | 합(p50 합) |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for cond in map(tuple, meta["conditions"]):
        if cond[2] not in ("perconn", "perconn10"):
            continue
        for variant in meta["variants"]:
            vals = [median_of(cells, cond, variant, f"phases_us.{p}.p50") for p in PHASES]
            total = sum(v for v in vals if v is not None)
            out.append(
                f"| {cond[0]} | {cond[1]} | {cond[2]} | `{variant}` | "
                + " | ".join(num(v, 1) for v in vals)
                + f" | {num(total, 1)} |"
            )
    return out


def memory_table(meta: dict, cells: list[dict], name: str, scale: float, digits: int) -> list[str]:
    variants = meta["variants"]
    out = [
        "| 크기 | 스레드 | 패턴 | " + " | ".join(f"`{v}`" for v in variants) + " |",
        "|---|---:|---|" + "---:|" * len(variants),
    ]
    for cond in map(tuple, meta["conditions"]):
        vals = [max_of(cells, cond, v, name) for v in variants]
        out.append(
            cond_label(cond)
            + "| "
            + " | ".join("–" if v is None else num(v / scale, digits) for v in vals)
            + " |"
        )
    return out


MECH_COLUMNS = (
    ("query_mean_us", "요청 평균 µs", 1, 1),
    ("close_mean_us", "close 평균 µs", 1, 1),
    ("minflt_per_op", "요청당 minor fault", 1, 2),
    ("sqlite.pagecache_overflow.highwater", "pcache malloc 최고 MiB", MIB, 1),
    ("sqlite.memory_used.highwater", "SQLite 메모리 최고 MiB", MIB, 1),
    ("sqlite.malloc_count.highwater", "할당 수 최고", 1, 0),
)


def mech_table(meta: dict, cells: list[dict]) -> list[str]:
    """``cache_mech`` 의 칸별 계측(블록 중앙값)."""
    cols = [c for c in MECH_COLUMNS if any(value(x, c[0]) is not None for x in cells)]
    out = [
        "| 요청 | 스레드 | 패턴 | 설정 | ops/s | " + " | ".join(c[1] for c in cols) + " |",
        "|---|---:|---|---|---:|" + "---:|" * len(cols),
    ]
    for cond in map(tuple, meta["conditions"]):
        for variant in meta["variants"]:
            vals = [median_of(cells, cond, variant, c[0]) for c in cols]
            ops = median_of(cells, cond, variant, "ops_per_s")
            out.append(
                f"{cond_label(cond)}| `{variant}` | {num(ops)} | "
                + " | ".join(
                    "–" if v is None else num(v / c[2], c[3])
                    for v, c in zip(vals, cols, strict=True)
                )
                + " |"
            )
    return out


def errors(cells: list[dict]) -> int:
    return sum(sum(c.get("errors", {}).values()) for c in cells)


def bench_table(path: Path) -> list[str]:
    """랩 벤치 원자료(``bench-<시각>.json``)의 ``mixed``·``CONN_MAX_AGE=0`` 요청 종류별 지연."""
    data = json.loads(path.read_text(encoding="utf-8"))
    present = {r["variant"] for r in data["runs"]}
    variants = [v for v in ("cache_size", "mmap_size", "all") if v in present]
    out = [
        f"`{path.name}`",
        "",
        "| 변형 | 반복 | 지연 출처 | Δ읽기 % | Δ정렬 % | Δ쓰기 % "
        "| 정렬/읽기 비 (변형) | (같은 반복 기준) |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in bench_kind_rows(data, variants):
        out.append(
            f"| `{row['variant']}` | {row['rep']} | {row['source']} | {pct(row['d_read'])} "
            f"| {pct(row['d_sorted'])} | {pct(row['d_write'])} | {row['ratio']:.3f} "
            f"| {row['base_ratio']:.3f} |"
        )
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 64
    if argv[0] == "--bench":
        out = ["### 랩 벤치 원자료: 요청 종류별 평균 지연(`mixed`, `CONN_MAX_AGE=0`)", ""]
        for p in argv[1:]:
            out += [*bench_table(Path(p)), ""]
        print("\n".join(out))
        return 0
    runs = [load(Path(p)) for p in argv]
    out = ["### 환경", "", *env_table(runs), ""]
    for meta, cells in runs:
        env = meta["env"]
        out += [
            f"### {env}: 처리량·p99 짝 차이(기준 대비)",
            "",
            f"칸 {len(cells)}개, 오류 {errors(cells)}건. 판정은 `_cachestat.verdict`(1차)·"
            "`same_direction`(보조).",
            "",
            *verdict_table(meta, cells),
            "",
        ]
        if meta.get("mech"):
            out += [f"### {env}: 계측(칸별 값의 블록 중앙값)", "", *mech_table(meta, cells), ""]
            continue
        out += [
            f"### {env}: 연결마다 열고 닫을 때 단계별 시간(µs, 칸별 p50 의 블록 중앙값)",
            "",
            *phase_table(meta, cells),
            "",
        ]
        if meta["sqlite_status"]:
            out += [
                f"### {env}: SQLite 메모리 최고치(MiB, `sqlite3_status` MEMORY_USED highwater, "
                "칸별 값의 최대)",
                "",
                *memory_table(meta, cells, "sqlite_mem.highwater", MIB, 1),
                "",
            ]
        out += [
            f"### {env}: 칸 끝 RSS(MiB, 칸별 값의 최대)",
            "",
            *memory_table(meta, cells, "rss_after", MIB, 0),
            "",
        ]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
