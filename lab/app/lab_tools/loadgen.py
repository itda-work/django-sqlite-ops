"""PRAGMA 벤치 부하 발생기. 앱과 같은 compose 네트워크의 별도 컨테이너에서 돈다.

닫힌 루프: 스레드 ``--concurrency`` 개가 keep-alive 연결 하나씩으로 요청을 보내고, 응답을 받으면
다음 요청을 보낸다. 요청 종류는 비율로 고른다(읽기 ``/lab/read/<id>``, 정렬 ``/lab/sorted``,
쓰기 ``POST /lab/write``). 요청마다 지연을 재고, 1초마다 ``/lab/stats`` 로 ``-wal`` 크기를 표본한다.
결과는 JSON 한 줄.

표준 라이브러리만 쓴다(이미지에 도구를 더 넣지 않는다). Python 클라이언트의 한계 처리량이 앱보다
충분히 높은지는 결과 문서에 적는다.
"""

import argparse
import http.client
import json
import random
import statistics
import threading
import time
from urllib.parse import urlsplit


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://labapp:8000")
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--warmup", type=float, default=3)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--max-id", type=int, required=True)
    p.add_argument("--read", type=float, default=0.7)
    p.add_argument("--sorted", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()

    target = urlsplit(args.url)
    start = time.monotonic()
    measure_from = start + args.warmup
    end = measure_from + args.duration
    lat: dict[str, list[float]] = {"read": [], "sorted": [], "write": []}
    errors: dict[str, int] = {"read": 0, "sorted": 0, "write": 0}
    lock = threading.Lock()
    wal: list[int] = []

    def worker(i: int) -> None:
        rng = random.Random(args.seed * 1000 + i)
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
        while True:
            now = time.monotonic()
            if now >= end:
                break
            r = rng.random()
            if r < args.read:
                kind, method, path = "read", "GET", f"/lab/read/{rng.randint(1, args.max_id)}"
            elif r < args.read + args.sorted:
                kind, method, path = "sorted", "GET", "/lab/sorted"
            else:
                kind, method, path = "write", "POST", "/lab/write?n=1"
            t0 = time.perf_counter()
            ok = True
            try:
                conn.request(method, path, body=b"" if method == "POST" else None)
                resp = conn.getresponse()
                resp.read()
                ok = resp.status == 200
            except (OSError, http.client.HTTPException):
                ok = False
                conn.close()
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
            dt = time.perf_counter() - t0
            if now >= measure_from:
                with lock:
                    if ok:
                        lat[kind].append(dt)
                    else:
                        errors[kind] += 1
        conn.close()

    def sampler() -> None:
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
        while time.monotonic() < end:
            try:
                conn.request("GET", "/lab/stats")
                body = json.loads(conn.getresponse().read())
                size = body["sizes"].get("db-wal")
                if size is not None:
                    wal.append(size)
            except (OSError, http.client.HTTPException, ValueError):
                conn.close()
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
            time.sleep(1)
        conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.concurrency)]
    threads.append(threading.Thread(target=sampler))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = {"duration": args.duration, "concurrency": args.concurrency, "kinds": {}}
    total = 0
    every: list[float] = []
    for kind, values in lat.items():
        total += len(values)
        every += values
        out["kinds"][kind] = {
            "n": len(values),
            "errors": errors[kind],
            "p50_ms": round(1000 * (percentile(values, 0.5) or 0), 2),
            "p99_ms": round(1000 * (percentile(values, 0.99) or 0), 2),
            "mean_ms": round(1000 * statistics.fmean(values), 2) if values else None,
        }
    out["rps"] = round(total / args.duration, 1)
    out["p50_ms"] = round(1000 * (percentile(every, 0.5) or 0), 2)
    out["p99_ms"] = round(1000 * (percentile(every, 0.99) or 0), 2)
    out["errors"] = sum(errors.values())
    out["wal_max"] = max(wal) if wal else None
    out["wal_last"] = wal[-1] if wal else None
    print(json.dumps(out))


if __name__ == "__main__":
    main()
