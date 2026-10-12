"""``cache_size`` 단독 악화(#27)의 최소 재현. 표준 라이브러리 ``sqlite3`` 만 쓴다.

랩(Django·uvicorn·Litestream·부하 발생기) 없이, 랩 벤치 ``mixed`` 와 같은 쿼리를 연결마다 열고
닫으며(``CONN_MAX_AGE=0`` 과 같은 패턴) 또는 한 연결로 반복하며 PRAGMA 변형을 비교한다.

    python lab/cache_probe.py --db-dir <새 디렉터리> --out <결과.jsonl> [--quick]

- 조건: DB 크기(``small`` 행 100,000 / ``large`` 64 MiB 를 넘는 행 수) × 스레드(1, 16) × 연결
  패턴(``perconn`` 요청마다 열고 닫기 / ``reuse`` 스레드마다 한 연결).
- 조건마다 템플릿 DB 를 새 하위 디렉터리로 복사하고, 버리는 워밍업 칸 하나 뒤에 블록 ``--blocks``
  개를 돈다. 블록마다 변형 순서를 무작위로 섞는다(블록 번호로 시드 고정). 칸 하나는 ``--cell``
  초 동안 닫힌 루프로 요청을 낸다.
- 요청 하나(랩 ``notes`` 뷰의 쿼리): 읽기 70%(pk 로 1행), 정렬 10%(인덱스 없는 열로 정렬, 임시
  B-트리), 1행 쓰기 20%(``BEGIN IMMEDIATE`` … ``RETURNING``). ``perconn`` 은 요청마다
  open → PRAGMA(Django 가 거는 두 줄 + 권장 PRAGMA + 변형) → 요청 → close 각각의 시간을 잰다.
- 조건 내내 baseline PRAGMA 로 연 연결 하나(holder)를 열어 둔다(랩의 Litestream 대역). 없으면
  단일 스레드 ``perconn`` 의 close 가 매번 마지막 연결 닫기가 되어 체크포인트·``-wal`` 삭제가
  끼어든다(``--no-holder`` 로 끈다).
- 출력은 JSON 한 줄씩: ``meta`` 한 줄, 칸마다 ``cell``. 집계·판정은 ``_cachestat.py``,
  표는 ``cache_report.py`` 가 만든다.

기본 ``pytest``·CI 에서 돌지 않는다(``test_*`` 가 아니고 ``tests/`` 밖이다). Linux 컨테이너에서는
``scripts/lab.sh cache`` 로 돈다.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import math
import os
import platform
import random
import resource
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# 이 패키지의 권장 PRAGMA(django_sqlite_ops/database.py 의 표). 컨테이너에서도 import 없이 돌도록
# 값을 옮겨 적고, tests/test_lab.py 가 표와 같은지 검사한다.
RECOMMENDED_PRAGMAS = {"journal_mode": "WAL", "synchronous": "NORMAL", "busy_timeout": 5000}
# Django 6.1 sqlite3 백엔드 get_new_connection 이 init_command 앞에 거는 것.
DJANGO_PRAGMAS = ("PRAGMA foreign_keys = ON", "PRAGMA legacy_alter_table = OFF")

VARIANTS = {
    "baseline": {},
    "cache_size": {"cache_size": -65536},  # 64 MiB, 랩 벤치와 같은 값
    "mmap_size": {"mmap_size": 268435456},  # 256 MiB
    "cache_mmap": {"cache_size": -65536, "mmap_size": 268435456},
    "all4": {
        "temp_store": "MEMORY",
        "mmap_size": 268435456,
        "cache_size": -65536,
        "journal_size_limit": 67108864,
    },
}
SIZES = {"small": 100_000, "large": 400_000}  # large: 약 100 MiB(> 64 MiB 캐시), meta 에 실제 크기
THREADS = (1, 16)
MODES = ("perconn", "reuse")
READ_SHARE, SORT_SHARE = 0.7, 0.1  # 나머지 0.2 는 1행 쓰기
BODY = "x" * 200
PHASES = ("open", "pragma", "query", "close")

SQL_READ = 'SELECT "id", "score", "body" FROM "notes_note" WHERE "id" = ? ORDER BY "id" ASC LIMIT 1'
SQL_SORT = (
    'SELECT "id" FROM "notes_note" WHERE ("score" >= ? AND "score" < ?) '
    'ORDER BY "body" ASC, "created" DESC LIMIT 20'
)
SQL_WRITE = 'INSERT INTO "notes_note" ("body", "score", "created") VALUES (?, ?, ?) RETURNING "id"'


def pragma_statements(variant: str) -> list[str]:
    """연결 직후 실행할 PRAGMA 문(Django 순서: 백엔드 두 줄 → init_command)."""
    merged = dict(RECOMMENDED_PRAGMAS)
    merged.update(VARIANTS[variant])
    return [*DJANGO_PRAGMAS, *(f"PRAGMA {k}={v}" for k, v in merged.items())]


# --- 메모리 ---------------------------------------------------------------------------------


def _sqlite_lib():
    """``sqlite3`` 모듈이 쓰는 바로 그 SQLite 인스턴스의 ctypes 핸들, 아니면 ``None``.

    macOS 의 uv Python 은 SQLite 를 정적으로 넣어 ctypes 로 닿지 않는다(``CDLL(None)`` 은 시스템
    사본 3.51.0 을 잡는다, 확인함). 판이 같고, 연결을 연 뒤 ``sqlite3_memory_used`` 가 0 보다 클
    때만 같은 인스턴스로 본다.
    """
    names = [ctypes.util.find_library("sqlite3"), "libsqlite3.so.0", None]
    probe = sqlite3.connect(":memory:")
    probe.execute("CREATE TABLE t(x)")
    try:
        for name in names:
            try:
                lib = ctypes.CDLL(name)
                lib.sqlite3_libversion.restype = ctypes.c_char_p
                lib.sqlite3_memory_used.restype = ctypes.c_int64
            except (OSError, AttributeError):
                continue
            if lib.sqlite3_libversion().decode() != sqlite3.sqlite_version:
                continue
            if lib.sqlite3_memory_used() > 0:
                return lib
    finally:
        probe.close()
    return None


SQLITE = None
SQLITE_STATUS_MEMORY_USED = 0


def sqlite_memory(reset: bool = False) -> dict | None:
    if SQLITE is None:
        return None
    cur, hi = ctypes.c_int64(), ctypes.c_int64()
    rc = SQLITE.sqlite3_status64(
        SQLITE_STATUS_MEMORY_USED, ctypes.byref(cur), ctypes.byref(hi), int(reset)
    )
    return None if rc else {"used": cur.value, "highwater": hi.value}


def rss_bytes() -> int | None:
    """현재 RSS. Linux 는 ``/proc/self/status``, 그 밖에는 ``ps``."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True
        )
        return int(out.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def maxrss_bytes() -> int:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v if sys.platform == "darwin" else v * 1024


def cgroup_memory_max() -> str | None:
    try:
        return Path("/sys/fs/cgroup/memory.max").read_text().strip()
    except OSError:
        return None


# --- DB -------------------------------------------------------------------------------------


def seed(path: Path, rows: int) -> None:
    """랩 ``lab_seed`` 와 같은 스키마·시드(``Random(10)``, 본문 200자)."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        'CREATE TABLE "notes_note" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
        '"body" text NOT NULL, "score" integer NOT NULL, "created" datetime NOT NULL)'
    )
    conn.execute('CREATE INDEX "notes_note_score_idx" ON "notes_note" ("score")')
    rng = random.Random(10)
    body = "y" * 200
    done = 0
    while done < rows:
        n = min(5000, rows - done)
        conn.execute("BEGIN")
        conn.executemany(
            'INSERT INTO "notes_note" ("body", "score", "created") VALUES (?, ?, ?)',
            [(body, rng.randrange(1_000_000), "2026-10-12 00:00:00.000000") for _ in range(n)],
        )
        conn.execute("COMMIT")
        done += n
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True)
    shutil.copyfile(src, dst)


def connect(path: Path, variant: str) -> tuple[sqlite3.Connection, float, float]:
    t0 = time.perf_counter()
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    t1 = time.perf_counter()
    for stmt in pragma_statements(variant):
        conn.execute(stmt).fetchall()
    t2 = time.perf_counter()
    return conn, t1 - t0, t2 - t1


def request(conn: sqlite3.Connection, rng: random.Random, max_id: int) -> str:
    r = rng.random()
    if r < READ_SHARE:
        conn.execute(SQL_READ, (rng.randrange(1, max_id + 1),)).fetchall()
        return "read"
    if r < READ_SHARE + SORT_SHARE:
        lo = rng.randrange(1_000_000)
        conn.execute(SQL_SORT, (lo, lo + 20_000)).fetchall()
        return "sort"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            SQL_WRITE, (BODY, rng.randrange(1_000_000), "2026-10-12 00:00:00.000000")
        ).fetchall()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return "write"


# --- 칸 하나 --------------------------------------------------------------------------------


def _worker(path, variant, mode, clock, seed_, max_id, barrier, out):
    rng = random.Random(seed_)
    lat: list[float] = []
    phases = {p: [] for p in PHASES}
    kinds = {"read": 0, "sort": 0, "write": 0}
    errors: dict[str, int] = {}
    conn = None
    if mode == "reuse":
        conn, _, _ = connect(path, variant)
    barrier.wait()  # 모든 워커가 준비되면 barrier 의 action 이 출발·마감 시각을 정한다
    deadline = clock["deadline"]
    try:
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            try:
                if mode == "perconn":
                    c, t_open, t_pragma = connect(path, variant)
                    t1 = time.perf_counter()
                    try:
                        kinds[request(c, rng, max_id)] += 1
                        t2 = time.perf_counter()
                    finally:
                        c.close()
                    t3 = time.perf_counter()
                    phases["open"].append(t_open)
                    phases["pragma"].append(t_pragma)
                    phases["query"].append(t2 - t1)
                    phases["close"].append(t3 - t2)
                else:
                    kinds[request(conn, rng, max_id)] += 1
            except sqlite3.Error as e:
                key = type(e).__name__ + ": " + str(e)[:60]
                errors[key] = errors.get(key, 0) + 1
                continue
            lat.append(time.perf_counter() - t0)
    finally:
        if conn is not None:
            conn.close()
    out.append({"lat": lat, "phases": phases, "kinds": kinds, "errors": errors})


def quantile(sorted_values: list[float], q: float) -> float | None:
    """최근접 순위 분위수: 정렬된 값의 ceil(q·n) 번째(_soakstat 과 같은 방식)."""
    if not sorted_values:
        return None
    k = math.ceil(q * len(sorted_values)) - 1
    return sorted_values[max(0, min(len(sorted_values) - 1, k))]


def run_cell(path: Path, variant: str, mode: str, threads: int, seconds: float, seed_: int):
    c = sqlite3.connect(path)
    max_id = c.execute('SELECT max("id") FROM "notes_note"').fetchone()[0]
    c.close()
    sqlite_memory(reset=True)
    rss0 = rss_bytes()
    clock: dict[str, float] = {}

    def go():
        # reuse 의 연결 열기는 칸 시간에 넣지 않는다(모든 워커가 연 뒤에 출발).
        clock["start"] = time.perf_counter()
        clock["deadline"] = clock["start"] + seconds

    barrier = threading.Barrier(threads, action=go)
    out: list[dict] = []
    ts = [
        threading.Thread(
            target=_worker,
            args=(path, variant, mode, clock, seed_ * 1000 + i, max_id, barrier, out),
        )
        for i in range(threads)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - clock["start"]
    lat = sorted(v for o in out for v in o["lat"])
    phases = {p: sorted(v for o in out for v in o["phases"][p]) for p in PHASES}
    kinds = {k: sum(o["kinds"][k] for o in out) for k in ("read", "sort", "write")}
    errors: dict[str, int] = {}
    for o in out:
        for k, v in o["errors"].items():
            errors[k] = errors.get(k, 0) + v
    return {
        "ops": len(lat),
        "wall_s": wall,
        "ops_per_s": len(lat) / wall,
        "p50_us": _us(quantile(lat, 0.5)),
        "p99_us": _us(quantile(lat, 0.99)),
        "mean_us": _us(sum(lat) / len(lat)) if lat else None,
        "phases_us": {
            p: {
                "p50": _us(quantile(v, 0.5)),
                "p99": _us(quantile(v, 0.99)),
                "mean": _us(sum(v) / len(v)) if v else None,
            }
            for p, v in phases.items()
            if v
        },
        "kinds": kinds,
        "errors": errors,
        "rss_before": rss0,
        "rss_after": rss_bytes(),
        "maxrss": maxrss_bytes(),
        "sqlite_mem": sqlite_memory(),
    }


def _us(v: float | None) -> float | None:
    return None if v is None else round(v * 1e6, 2)


def block_order(block: int, variants: list[str]) -> list[str]:
    order = list(variants)
    random.Random(f"block-{block}").shuffle(order)
    return order


# --- 실행 -----------------------------------------------------------------------------------


def meta(db_dir: Path, templates: dict[str, Path], args) -> dict:
    c = sqlite3.connect(":memory:")
    opts = [r[0] for r in c.execute("PRAGMA compile_options")]
    c.close()
    sizes = {}
    for name, p in templates.items():
        with sqlite3.connect(p) as c:
            page_size = c.execute("PRAGMA page_size").fetchone()[0]
            pages = c.execute("PRAGMA page_count").fetchone()[0]
        c.close()
        sizes[name] = {
            "rows": SIZES[name],
            "bytes": p.stat().st_size,
            "page_size": page_size,
            "pages": pages,
        }
    return {
        "kind": "meta",
        "env": args.env,
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "sqlite": sqlite3.sqlite_version,
        "compile_options": opts,
        "sqlite_status": SQLITE is not None,
        "cgroup_memory_max": cgroup_memory_max(),
        "db_dir": str(db_dir),
        "sizes": sizes,
        "blocks": args.blocks,
        "holder": args.holder,
        "cell_s": args.cell,
        "variants": args.variants,
        "conditions": [list(c) for c in conditions(args)],
        "pragmas": {v: pragma_statements(v) for v in args.variants},
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def conditions(args):
    return [(s, t, m) for s in args.sizes for t in args.threads for m in args.modes]


def main(argv=None) -> int:
    global SQLITE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db-dir", required=True, type=Path, help="새로 만들 디렉터리(있으면 거부)")
    ap.add_argument("--out", required=True, help="결과 JSONL 경로, '-' 면 stdout")
    ap.add_argument("--env", default=sys.platform)
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument("--cell", type=float, default=1.5)
    ap.add_argument("--sizes", default="small,large")
    ap.add_argument("--threads", default=",".join(map(str, THREADS)))
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument(
        "--no-holder",
        dest="holder",
        action="store_false",
        help="조건 내내 열어 두는 연결(Litestream 대역)을 두지 않는다",
    )
    ap.add_argument("--quick", action="store_true", help="하네스 확인용(블록 2, 칸 0.3초, 행 1/10)")
    args = ap.parse_args(argv)
    args.sizes = args.sizes.split(",")
    args.threads = [int(t) for t in args.threads.split(",")]
    args.modes = args.modes.split(",")
    args.variants = args.variants.split(",")
    if args.variants[0] != "baseline":
        ap.error("--variants must start with baseline")
    if args.quick:
        args.blocks, args.cell = 2, 0.3
        for k in SIZES:
            SIZES[k] //= 10

    args.db_dir.mkdir(parents=True, exist_ok=False)
    SQLITE = _sqlite_lib()
    out = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")  # noqa: SIM115

    def emit(rec):
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()

    templates = {}
    for name in args.sizes:
        p = args.db_dir / "template" / name / "db.sqlite3"
        p.parent.mkdir(parents=True)
        t0 = time.perf_counter()
        seed(p, SIZES[name])
        print(
            f"seeded {name}: {p.stat().st_size:,} B in {time.perf_counter() - t0:.1f}s",
            file=sys.stderr,
        )
        templates[name] = p
    emit(meta(args.db_dir, templates, args))

    for ci, (size, threads, mode) in enumerate(conditions(args)):
        path = args.db_dir / f"c{ci:02d}-{size}-t{threads}-{mode}" / "db.sqlite3"
        copy_db(templates[size], path)
        cond = {"size": size, "threads": threads, "mode": mode}
        # 랩에서는 Litestream 이 DB 를 늘 열고 있다. 그 대역으로 조건 내내 연결 하나를 열어 둔다.
        # 없으면 연결마다 '마지막 연결 닫기'가 되어 SQLite 가 체크포인트하고 -wal·-shm 을 지운다.
        # 트랜잭션은 잡지 않는다(자동 체크포인트는 막지 않는다).
        holder = None
        if args.holder:
            holder, _, _ = connect(path, "baseline")
            holder.execute('SELECT count(*) FROM "notes_note"').fetchall()
        t0 = time.perf_counter()
        warm = run_cell(path, "baseline", mode, threads, args.cell, seed_=ci * 100_000)
        emit({"kind": "warmup", **cond, **warm})
        for b in range(args.blocks):
            for pos, variant in enumerate(block_order(b, args.variants)):
                r = run_cell(
                    path, variant, mode, threads, args.cell, seed_=ci * 100_000 + b * 100 + pos + 1
                )
                emit({"kind": "cell", **cond, "block": b, "pos": pos, "variant": variant, **r})
        if holder is not None:
            holder.close()
        print(f"{size} t{threads} {mode}: {time.perf_counter() - t0:.0f}s", file=sys.stderr)
    emit({"kind": "end", "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    if out is not sys.stdout:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
