"""#106 부하 발생기: 커밋된 행의 id 범위·WAL 시계열·구간별 지연을 모은다. JSON 한 줄을 낸다.

``loadgen`` 서비스 컨테이너에서 ``--entrypoint python`` 으로 돈다(앱과 같은 compose 네트워크).

- 쓰기 스레드 ``--writers`` 개: ``POST /lab/write?n=<rows>&size=<bytes>``. 200 응답의
  ``first_id``·``last_id`` 를 '앱이 커밋에 성공한 행'으로 모은다(응답을 못 받은 요청은
  커밋됐는지 알 수 없으므로 ``unknown`` 으로 따로 센다). ``--rate`` 가 있으면 전체 요청률
  (req/s)을 그 값으로 맞춘다.
- 읽기 스레드 ``--readers`` 개: ``GET /lab/read/<지금까지 커밋된 id 중 무작위>``.
- 표본 스레드: ``--sample`` 초마다 ``/lab/probe``(DB 를 열지 않음)의 ``-wal`` 크기·헤더 salt·
  ``-shm`` 의 mxFrame·nBackfill·nBackfillAttempted 를 시각(``time.time()``)과 함께 남긴다.
- 지연은 ``--interval`` 초 구간마다 쓰기·읽기 각각 n·오류·p50·p95(ms).

시각은 벽시계(``time.time()``)다. 하네스가 같은 시계로 사건(Litestream 정지 등)을 적어 맞춘다.
표준 라이브러리만 쓴다.
"""

import argparse
import http.client
import json
import random
import threading
import time
from urllib.parse import urlsplit


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(1000 * s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))], 2)


def merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    """겹치거나 이어진 [처음, 끝] 구간을 합친다."""
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://labapp:8000")
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--writers", type=int, default=2)
    p.add_argument("--readers", type=int, default=2)
    p.add_argument("--rows", type=int, default=10)
    p.add_argument("--size", type=int, default=3500)
    p.add_argument("--rate", type=float, default=0, help="쓰기 요청률 합계(req/s), 0 이면 무제한")
    p.add_argument("--sample", type=float, default=0.1)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()

    target = urlsplit(args.url)
    start = time.time()
    end = start + args.duration
    lock = threading.Lock()
    committed: list[list[int]] = []
    max_id = [0]
    unknown = [0]
    errors: dict[str, int] = {}
    error_sample: dict[str, str] = {}
    # (구간 번호, 종류) -> 지연 초 목록 / 오류 수
    lat: dict[tuple[int, str], list[float]] = {}
    errs: dict[tuple[int, str], int] = {}
    samples: list[dict] = []

    def conn() -> http.client.HTTPConnection:
        return http.client.HTTPConnection(target.hostname, target.port, timeout=60)

    def record(kind: str, t0: float, dt: float, why: str | None, body: str) -> None:
        k = (int((t0 - start) // args.interval), kind)
        with lock:
            if why is None:
                lat.setdefault(k, []).append(dt)
            else:
                errs[k] = errs.get(k, 0) + 1
                errors[f"{kind} {why}"] = errors.get(f"{kind} {why}", 0) + 1
                error_sample.setdefault(f"{kind} {why}", body[:300])

    def writer(i: int) -> None:
        c = conn()
        per = args.writers / args.rate if args.rate > 0 else 0
        nxt = time.time() + per * i / max(1, args.writers)
        while time.time() < end:
            if per:
                wait = nxt - time.time()
                if wait > 0:
                    time.sleep(wait)
                nxt += per
            t0 = time.time()
            why, body = None, ""
            try:
                c.request("POST", f"/lab/write?n={args.rows}&size={args.size}", body=b"")
                resp = c.getresponse()
                raw = resp.read()
                body = raw[:300].decode("utf-8", "replace")
                if resp.status == 200:
                    d = json.loads(raw)
                    with lock:
                        committed.append([d["first_id"], d["last_id"]])
                        max_id[0] = max(max_id[0], d["last_id"])
                else:
                    why = f"http {resp.status}"
            except (OSError, http.client.HTTPException, ValueError) as exc:
                why, body = type(exc).__name__, str(exc)
                with lock:
                    unknown[0] += 1
                c.close()
                c = conn()
            record("write", t0, time.time() - t0, why, body)
        c.close()

    def reader(i: int) -> None:
        rng = random.Random(args.seed * 1000 + i)
        c = conn()
        while time.time() < end:
            with lock:
                top = max_id[0]
            if top == 0:
                time.sleep(0.05)
                continue
            t0 = time.time()
            why, body = None, ""
            try:
                c.request("GET", f"/lab/read/{rng.randint(1, top)}")
                resp = c.getresponse()
                body = resp.read()[:300].decode("utf-8", "replace")
                if resp.status != 200:
                    why = f"http {resp.status}"
            except (OSError, http.client.HTTPException) as exc:
                why, body = type(exc).__name__, str(exc)
                c.close()
                c = conn()
            record("read", t0, time.time() - t0, why, body)
        c.close()

    def sampler() -> None:
        c = conn()
        while True:
            now = time.time()
            try:
                c.request("GET", "/lab/probe")
                s = json.loads(c.getresponse().read())
                wal, shm = s.get("wal") or {}, s.get("shm") or {}
                samples.append(
                    {
                        "t": round(now - start, 3),
                        "wal": s["sizes"].get("db-wal"),
                        "db": s["sizes"].get("db"),
                        "salt": [wal.get("salt1"), wal.get("salt2")] if wal else None,
                        "ckpt_seq": wal.get("ckpt_seq"),
                        "mx": shm.get("mx_frame"),
                        "nb": shm.get("n_backfill"),
                        "nba": shm.get("n_backfill_attempted"),
                    }
                )
            except (OSError, http.client.HTTPException, ValueError):
                c.close()
                c = conn()
            if now >= end:
                break
            time.sleep(args.sample)
        c.close()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(args.writers)]
    threads += [threading.Thread(target=reader, args=(i,)) for i in range(args.readers)]
    threads.append(threading.Thread(target=sampler))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    intervals = []
    n_int = int(args.duration // args.interval) + 1
    for i in range(n_int):
        row: dict = {"i": i, "t": i * args.interval}
        for kind in ("write", "read"):
            v = lat.get((i, kind), [])
            row[kind] = {
                "n": len(v),
                "errors": errs.get((i, kind), 0),
                "p50_ms": pct(v, 0.5),
                "p95_ms": pct(v, 0.95),
            }
        intervals.append(row)
    ranges = merge_ranges(committed)
    print(
        json.dumps(
            {
                "start_wall": start,
                "args": vars(args),
                "write_requests_ok": len(committed),
                "rows_committed": sum(hi - lo + 1 for lo, hi in ranges),
                "committed_ranges": ranges,
                "unknown_writes": unknown[0],
                "errors": errors,
                "error_sample": error_sample,
                "intervals": intervals,
                "samples": samples,
            }
        )
    )


if __name__ == "__main__":
    main()
