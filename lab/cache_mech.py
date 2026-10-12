"""``cache_size`` 악화의 원인 가설 측정(#27). ``cache_probe.py`` 결과로 세운 가설을 하나씩 잰다.

    python lab/cache_mech.py --db-dir <새 디렉터리> --out <결과.jsonl>

- H1(연결당 많이 읽는 요청): ``perconn``(요청마다 열고 닫기) × 요청 종류(``read`` pk 1행 /
  ``sort`` 인덱스 없는 정렬, 약 2,000 페이지) × 스레드(1, 16).
- H3(리뷰 1 반례, 한 연결에서 쿼리 여러 개): ``perconn`` × (``sort10same`` 같은 구간 정렬 10회 /
  ``sort10diff`` 다른 구간 10회) × 스레드(1, 16).
- H2(WAL 변경이 재사용 연결의 캐시를 비움): ``reuse`` 16 스레드 × (``sort`` 쓰기 없음 /
  ``sortwrite`` 정렬 80%·1행 쓰기 20%).
- 설정: ``cache_size`` (기본 -2000 / -8192 / -65536) × ``mmap_size`` (0 / 256 MiB).
- 조건마다 템플릿(행 100,000)을 새 하위 디렉터리로 복사하고 holder 연결을 열어 둔다. 워밍업 칸
  하나 뒤 블록마다 설정 순서를 섞는다. 칸마다 처리량, 요청·close 평균 시간, 요청당 minor page
  fault(``getrusage``, 프로세스 전체), Linux 에서는 ``sqlite3_status`` 의 MEMORY_USED·
  PAGECACHE_OVERFLOW(pcache 가 malloc 으로 받은 바이트)·MALLOC_COUNT 최고치를 남긴다.

워커 예외는 칸을 실패로 올리고 실행을 멈춘다(``cache_probe.raise_worker_failures``).
출력 형식은 ``cache_probe.py`` 와 같아서(``meta``·``warmup``·``cell``·``end``, 조건 =
``size``·``threads``·``mode``) ``cache_report.py`` 로 표를 만든다. 기본 ``pytest``·CI 에서
돌지 않는다.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import platform
import random
import resource
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cache_probe as cp  # noqa: E402

CONFIGS = {
    "baseline": {},
    "cache8m": {"cache_size": -8192},
    "cache_size": {"cache_size": -65536},
    "mmap_size": {"mmap_size": 268435456},
    "cache8m_mmap": {"cache_size": -8192, "mmap_size": 268435456},
    "cache_mmap": {"cache_size": -65536, "mmap_size": 268435456},
}
# (mode, kind, threads). kind 는 조건 표의 size 자리에 넣는다(cache_report 의 조건 열 재사용).
CONDITIONS = (
    ("perconn", "read", 1),
    ("perconn", "sort", 1),
    ("perconn", "read", 16),
    ("perconn", "sort", 16),
    ("perconn", "sort10same", 1),
    ("perconn", "sort10diff", 1),
    ("perconn", "sort10same", 16),
    ("perconn", "sort10diff", 16),
    ("reuse", "sort", 16),
    ("reuse", "sortwrite", 16),
)
STATUS = {"memory_used": 0, "pagecache_overflow": 2, "malloc_count": 9}


def statements(config: str) -> list[str]:
    merged = dict(cp.RECOMMENDED_PRAGMAS)
    merged.update(CONFIGS[config])
    return [*cp.DJANGO_PRAGMAS, *(f"PRAGMA {k}={v}" for k, v in merged.items())]


def open_conn(path: Path, config: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    for stmt in statements(config):
        conn.execute(stmt).fetchall()
    return conn


# 연결 하나에서 내는 쿼리 수(perconn). 리뷰 1 반례: 한 연결에서 정렬 여러 번.
QUERIES_PER_CONN = {"sort10same": 10, "sort10diff": 10}


def one(conn: sqlite3.Connection, kind: str, rng: random.Random, max_id: int, lo=None) -> None:
    if kind == "read":
        conn.execute(cp.SQL_READ, (rng.randrange(1, max_id + 1),)).fetchall()
        return
    if kind == "sortwrite" and rng.random() < 0.2:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(cp.SQL_WRITE, (cp.BODY, rng.randrange(1_000_000), "2026-10-12")).fetchall()
        conn.execute("COMMIT")
        return
    if lo is None:
        lo = rng.randrange(1_000_000)
    conn.execute(cp.SQL_SORT, (lo, lo + 20_000)).fetchall()


def status(reset: bool) -> dict | None:
    if cp.SQLITE is None:
        return None
    out = {}
    for name, op in STATUS.items():
        cur, hi = ctypes.c_int64(), ctypes.c_int64()
        if cp.SQLITE.sqlite3_status64(op, ctypes.byref(cur), ctypes.byref(hi), int(reset)) == 0:
            out[name] = {"used": cur.value, "highwater": hi.value}
    return out


def worker(path, config, mode, kind, clock, seed_, max_id, barrier, out, failures):
    """어떤 예외든 ``failures`` 에 넣고 barrier 를 깬다. ``run_cell`` 이 칸을 실패로 올린다(리뷰 1).

    ``perconn`` 은 연결 하나에 쿼리 ``QUERIES_PER_CONN[kind]``(기본 1)개. ``sort10same`` 은 같은
    구간을 10번, ``sort10diff`` 는 매번 다른 구간을 정렬한다.
    """
    rng = random.Random(seed_)
    q, cl, n, nc = 0.0, 0.0, 0, 0
    per_conn = QUERIES_PER_CONN.get(kind, 1)
    conn = None
    try:
        if mode == "reuse":
            conn = open_conn(path, config)
        barrier.wait()
        deadline = clock["deadline"]
        while time.perf_counter() < deadline:
            if mode == "perconn":
                c = open_conn(path, config)
                try:
                    same = rng.randrange(1_000_000) if kind == "sort10same" else None
                    t1 = time.perf_counter()
                    for _ in range(per_conn):
                        one(c, kind, rng, max_id, lo=same)
                    t2 = time.perf_counter()
                finally:
                    c.close()
                cl += time.perf_counter() - t2
                nc += 1
            else:
                t1 = time.perf_counter()
                one(conn, kind, rng, max_id)
                t2 = time.perf_counter()
            q += t2 - t1
            n += per_conn if mode == "perconn" else 1
        out.append((n, q, cl, nc))
    except BaseException as e:
        failures.append(e)
        barrier.abort()
    finally:
        if conn is not None:
            conn.close()


def run_cell(path, config, mode, kind, threads, seconds, seed_):
    c = sqlite3.connect(path)
    max_id = c.execute('SELECT max("id") FROM "notes_note"').fetchone()[0]
    c.close()
    status(reset=True)
    clock: dict[str, float] = {}

    def go():
        clock["ru"] = resource.getrusage(resource.RUSAGE_SELF).ru_minflt
        clock["start"] = time.perf_counter()
        clock["deadline"] = clock["start"] + seconds

    barrier = threading.Barrier(threads, action=go)
    out: list = []
    failures: list[BaseException] = []
    ts = [
        threading.Thread(
            target=worker,
            args=(
                path,
                config,
                mode,
                kind,
                clock,
                seed_ * 1000 + i,
                max_id,
                barrier,
                out,
                failures,
            ),
        )
        for i in range(threads)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    cp.raise_worker_failures(failures)
    wall = time.perf_counter() - clock["start"]
    minflt = resource.getrusage(resource.RUSAGE_SELF).ru_minflt - clock["ru"]
    n = sum(o[0] for o in out)
    nc = sum(o[3] for o in out)
    return {
        "ops": n,
        "wall_s": wall,
        "ops_per_s": n / wall,
        "query_mean_us": round(1e6 * sum(o[1] for o in out) / n, 2) if n else None,
        # close 는 연결당 평균(쿼리 여러 개인 조건에서도 연결 하나에 한 번).
        "close_mean_us": round(1e6 * sum(o[2] for o in out) / nc, 2) if nc else None,
        "minflt_per_op": round(minflt / n, 3) if n else None,
        "sqlite": status(reset=False),
        "rss_after": cp.rss_bytes(),
        "errors": {},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db-dir", required=True, type=Path, help="새로 만들 디렉터리(있으면 거부)")
    ap.add_argument("--out", required=True, help="결과 JSONL 경로, '-' 면 stdout")
    ap.add_argument("--env", default=sys.platform)
    ap.add_argument("--blocks", type=int, default=6)
    ap.add_argument("--cell", type=float, default=1.0)
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--quick", action="store_true", help="하네스 확인용(블록 1, 칸 0.2초, 행 1/10)")
    args = ap.parse_args(argv)
    if args.quick:
        args.blocks, args.cell, args.rows = 1, 0.2, args.rows // 10

    args.db_dir.mkdir(parents=True, exist_ok=False)
    cp.SQLITE = cp._sqlite_lib()
    out = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")  # noqa: SIM115

    def emit(rec):
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()

    template = args.db_dir / "template" / "db.sqlite3"
    template.parent.mkdir(parents=True)
    cp.seed(template, args.rows)
    variants = list(CONFIGS)
    emit(
        {
            "kind": "meta",
            "env": args.env,
            "probe_sha256": hashlib.sha256(
                Path(__file__).read_bytes() + Path(cp.__file__).read_bytes()
            ).hexdigest(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpus": __import__("os").cpu_count(),
            "sqlite": sqlite3.sqlite_version,
            "sqlite_status": cp.SQLITE is not None,
            "cgroup_memory_max": cp.cgroup_memory_max(),
            "sizes": {"template": {"rows": args.rows, "bytes": template.stat().st_size}},
            "blocks": args.blocks,
            "cell_s": args.cell,
            "holder": True,
            "mech": True,
            "variants": variants,
            "conditions": [[kind, threads, mode] for mode, kind, threads in CONDITIONS],
            "pragmas": {v: statements(v) for v in variants},
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    )
    for ci, (mode, kind, threads) in enumerate(CONDITIONS):
        path = args.db_dir / f"c{ci:02d}-{mode}-{kind}-t{threads}" / "db.sqlite3"
        cp.copy_db(template, path)
        holder = open_conn(path, "baseline")
        holder.execute('SELECT count(*) FROM "notes_note"').fetchall()
        cond = {"size": kind, "threads": threads, "mode": mode}
        t0 = time.perf_counter()
        emit(
            {
                "kind": "warmup",
                **cond,
                **run_cell(path, "baseline", mode, kind, threads, args.cell, ci * 100_000),
            }
        )
        for b in range(args.blocks):
            for pos, config in enumerate(cp.block_order(b, variants)):
                r = run_cell(
                    path, config, mode, kind, threads, args.cell, ci * 100_000 + b * 100 + pos + 1
                )
                emit({"kind": "cell", **cond, "block": b, "pos": pos, "variant": config, **r})
        holder.close()
        print(f"{mode} {kind} t{threads}: {time.perf_counter() - t0:.0f}s", file=sys.stderr)
    emit({"kind": "end", "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    if out is not sys.stdout:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
