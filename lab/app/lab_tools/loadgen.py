"""PRAGMA 벤치 부하 발생기. 앱과 같은 compose 네트워크의 별도 컨테이너에서 돈다.

닫힌 루프: 스레드 ``--concurrency`` 개가 keep-alive 연결 하나씩으로 요청을 보내고, 응답을 받으면
다음 요청을 보낸다. ``--mode mixed`` 는 요청 종류를 비율로 고른다(읽기 ``/lab/read/<id>``, 정렬
``/lab/sorted``, 나머지는 쓰기 ``POST /lab/write?n=<--write-rows>``). ``--mode ping`` 은 DB 없는
Django 뷰, ``--mode rawping`` 은 Django 앞에서 바로 답하는 경로만 부른다(발생기·스택 한계 확인).

요청마다 클라이언트 지연과 서버 헤더(``X-Lab-View-Us`` 뷰 본문, ``X-Lab-App-Us`` ASGI 래퍼에서
응답 시작까지)를 모은다. 세 분포의 중앙값끼리 빼서 구간을 나누면 안 되므로(중앙값의 차 ≠ 차의
중앙값), 같은 요청 안의 차이(``gap.client_minus_app``, ``gap.app_minus_view``)를 요청마다 계산해
그 분포를 따로 낸다. ``--sample`` 초마다 ``/lab/probe``(DB 를 열지 않음)로 ``-wal`` 크기·헤더와
서버 카운터를 표본한다. 측정 구간의 시작·끝 표본으로 연결 생성 수·DB 요청 수·서버 CPU 시간의
차이를 내고, 시작·끝 WAL 헤더는 그대로 남긴다. 발생기 자신의 CPU 시간도 잰다(여유 확인).
결과는 JSON 한 줄.

표준 라이브러리만 쓴다(이미지에 도구를 더 넣지 않는다).
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


def dist_ms(values: list[float]) -> dict:
    """초 단위 값들의 p50·p99·평균(ms)."""
    return {
        "p50_ms": round(1000 * (percentile(values, 0.5) or 0), 3),
        "p99_ms": round(1000 * (percentile(values, 0.99) or 0), 3),
        "mean_ms": round(1000 * statistics.fmean(values), 3) if values else None,
    }


def probe_delta(first: dict | None, last: dict | None, samples: list[dict]) -> dict:
    """측정 구간 처음·끝 ``/lab/probe`` 표본의 차이(``samples`` 는 측정 구간 표본)."""
    if not first or not last:
        return {}
    wall = last["t"] - first["t"]
    out = {
        "window_s": round(wall, 2),
        "conn_created": last["conn_created"] - first["conn_created"],
        "db_requests": last["db_requests"] - first["db_requests"],
        "server_cpu_util": round((last["cpu_s"] - first["cpu_s"]) / wall, 3) if wall > 0 else None,
        "db_fds_end": last["db_fds"],
        "db_fds_max": max(s["db_fds"] or 0 for s in samples),
        "fd_limit": last.get("fd_limit"),
        "threads_end": last["threads"],
    }
    out["conn_per_db_request"] = (
        round(out["conn_created"] / out["db_requests"], 4) if out["db_requests"] else None
    )
    # WAL 재시작 횟수는 세지 않는다. 헤더의 ckpt_seq 는 헤더를 쓴 연결 핸들의 카운터라 여러
    # 연결(요청마다 새 앱 연결, Litestream)이 재시작하면 횟수가 아니다(#26 리뷰 1, 재현함).
    # 날것의 차이와, salt 가 바뀐 표본 간격 수(그 사이 헤더가 한 번 이상 다시 쓰였다는 하한)만 낸다.
    out["wal_head_start"] = first.get("wal")
    out["wal_head_end"] = last.get("wal")
    if first.get("wal") and last.get("wal"):
        out["wal_ckpt_seq_delta"] = last["wal"]["ckpt_seq"] - first["wal"]["ckpt_seq"]
    heads = [s["wal"] for s in [first, *samples] if s.get("wal")]
    out["wal_salt_changes"] = sum(
        1
        for a, b in zip(heads, heads[1:], strict=False)
        if (a["salt1"], a.get("salt2")) != (b["salt1"], b.get("salt2"))
    )
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://labapp:8000")
    p.add_argument("--mode", choices=("mixed", "ping", "rawping"), default="mixed")
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--warmup", type=float, default=3)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--max-id", type=int, default=1)
    p.add_argument("--read", type=float, default=0.7)
    p.add_argument("--sorted", type=float, default=0.1)
    p.add_argument("--write-rows", type=int, default=1)
    p.add_argument("--sample", type=float, default=0.5)
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()

    target = urlsplit(args.url)
    start = time.monotonic()
    measure_from = start + args.warmup
    end = measure_from + args.duration
    kinds = ("read", "sorted", "write", "ping")
    lat: dict[str, list[float]] = {k: [] for k in kinds}
    view: dict[str, list[float]] = {k: [] for k in kinds}
    app: dict[str, list[float]] = {k: [] for k in kinds}
    # 같은 요청 안의 차이(초). 헤더가 둘 다 있을 때만.
    client_minus_app: dict[str, list[float]] = {k: [] for k in kinds}
    app_minus_view: dict[str, list[float]] = {k: [] for k in kinds}
    errors: dict[str, int] = dict.fromkeys(kinds, 0)
    # 오류 종류(상태 코드 또는 예외 이름)별 수와 첫 응답 본문 일부
    error_kinds: dict[str, int] = {}
    error_sample: dict[str, str] = {}
    lock = threading.Lock()
    samples: list[dict] = []

    def pick(rng: random.Random) -> tuple[str, str, str]:
        if args.mode == "ping":
            return "ping", "GET", "/lab/ping"
        if args.mode == "rawping":
            return "ping", "GET", "/lab/rawping"
        r = rng.random()
        if r < args.read:
            return "read", "GET", f"/lab/read/{rng.randint(1, args.max_id)}"
        if r < args.read + args.sorted:
            return "sorted", "GET", "/lab/sorted"
        return "write", "POST", f"/lab/write?n={args.write_rows}"

    def worker(i: int) -> None:
        rng = random.Random(args.seed * 1000 + i)
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
        while True:
            now = time.monotonic()
            if now >= end:
                break
            kind, method, path = pick(rng)
            t0 = time.perf_counter()
            ok = True
            v_us = a_us = None
            why = body = ""
            try:
                conn.request(method, path, body=b"" if method == "POST" else None)
                resp = conn.getresponse()
                body = resp.read()[:300].decode("utf-8", "replace")
                ok = resp.status == 200
                why = f"http {resp.status}"
                v_us = resp.getheader("X-Lab-View-Us")
                a_us = resp.getheader("X-Lab-App-Us")
            except (OSError, http.client.HTTPException) as exc:
                ok = False
                why, body = type(exc).__name__, str(exc)[:300]
                conn.close()
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
            dt = time.perf_counter() - t0
            if now >= measure_from:
                with lock:
                    if ok:
                        lat[kind].append(dt)
                        v = int(v_us) / 1e6 if v_us is not None else None
                        a = int(a_us) / 1e6 if a_us is not None else None
                        if v is not None:
                            view[kind].append(v)
                        if a is not None:
                            app[kind].append(a)
                            client_minus_app[kind].append(dt - a)
                            if v is not None:
                                app_minus_view[kind].append(a - v)
                    else:
                        errors[kind] += 1
                        error_kinds[why] = error_kinds.get(why, 0) + 1
                        error_sample.setdefault(why, body)
        conn.close()

    def sampler() -> None:
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
        while True:
            now = time.monotonic()
            try:
                conn.request("GET", "/lab/probe")
                body = json.loads(conn.getresponse().read())
                body["phase"] = "warmup" if now < measure_from else "measure"
                samples.append(body)
            except (OSError, http.client.HTTPException, ValueError):
                conn.close()
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
            if now >= end:
                break
            time.sleep(min(args.sample, max(0.0, end - time.monotonic())) or 0.01)
        conn.close()

    cpu0 = time.process_time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.concurrency)]
    threads.append(threading.Thread(target=sampler))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    gen_cpu = time.process_time() - cpu0
    gen_wall = time.monotonic() - start

    out: dict = {
        "mode": args.mode,
        "duration": args.duration,
        "concurrency": args.concurrency,
        "write_rows": args.write_rows,
        "kinds": {},
    }
    total = 0
    every: list[float] = []
    every_view: list[float] = []
    every_app: list[float] = []
    every_cma: list[float] = []
    every_amv: list[float] = []
    for kind in kinds:
        if not lat[kind] and not errors[kind]:
            continue
        total += len(lat[kind])
        every += lat[kind]
        every_view += view[kind]
        every_app += app[kind]
        every_cma += client_minus_app[kind]
        every_amv += app_minus_view[kind]
        out["kinds"][kind] = {
            "n": len(lat[kind]),
            "errors": errors[kind],
            **dist_ms(lat[kind]),
            "view": dist_ms(view[kind]),
            "app": dist_ms(app[kind]),
            "gap": {
                "client_minus_app": dist_ms(client_minus_app[kind]),
                "app_minus_view": dist_ms(app_minus_view[kind]),
            },
        }
    out["rps"] = round(total / args.duration, 1)
    out.update({k: v for k, v in dist_ms(every).items()})
    out["view"] = dist_ms(every_view)
    out["app"] = dist_ms(every_app)
    out["gap"] = {
        "client_minus_app": dist_ms(every_cma),
        "app_minus_view": dist_ms(every_amv),
    }
    out["errors"] = sum(errors.values())
    out["error_kinds"] = error_kinds
    out["error_sample"] = error_sample
    # 발생기 CPU: 단일 프로세스(GIL)라 1.0 에 가까우면 발생기가 한계다.
    out["gen_cpu_util"] = round(gen_cpu / gen_wall, 3) if gen_wall > 0 else None

    measured = [s for s in samples if s["phase"] == "measure"]
    before = [s for s in samples if s["phase"] == "warmup"]
    first = before[-1] if before else (measured[0] if measured else None)
    last = measured[-1] if measured else None
    wal_sizes = [s["sizes"]["db-wal"] for s in measured if s["sizes"].get("db-wal") is not None]
    out["wal_start"] = first["sizes"].get("db-wal") if first else None
    out["wal_max"] = max(wal_sizes) if wal_sizes else None
    out["wal_end"] = last["sizes"].get("db-wal") if last else None
    out["wal_shrinks"] = sum(1 for a, b in zip(wal_sizes, wal_sizes[1:], strict=False) if b < a)
    out["samples"] = len(measured)
    out["server"] = probe_delta(first, last, measured)
    out["wal_series"] = wal_sizes
    print(json.dumps(out))


if __name__ == "__main__":
    main()
