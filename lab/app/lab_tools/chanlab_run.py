"""채널 레이어 전달 시나리오 하나를 돌리고 원자료를 JSON 한 줄로 낸다(#66, P2).

앱 컨테이너 안에서 별도 프로세스로 돈다(``docker compose exec app timeout … python …``). 워커의
수신 루프는 ``notes/chanlab.py`` 뷰가 띄우고, 이 스크립트가 ``http://127.0.0.1:8000`` 으로
두 워커를 모두 볼 때까지 되풀이해 부른다. 순서:

1. ``listen`` — 워커 둘 다 수신 루프를 띄운다(서로 다른 PID 둘).
2. 수신 준비 — probe 를 보내며 두 워커가 모두 probe 를 1건 이상 받을 때까지 기다린다.
3. 본 메시지 ``n`` 건(``i`` = 0..n-1) — 이 프로세스에서 보내거나(``sender: process``) 워커가
   보낸다(``sender: worker``, 수신 워커가 아닌 쪽).
4. 수신이 멈출 때까지 기다리고, 꼬리 probe(용량 드롭 합계 로그용)를 보내 다시 기다린 뒤
   ``stop``(``keep`` 이면 남겨 둔다).
   ``may_not_ready`` 면 준비 기한(``ready_deadline``)을 넘겨도 실패하지 않고 ``ready: false`` 로
   남긴다(본 메시지는 보내지 않는다).
5. ``close`` 면 ``await layer.close()`` 시간을 잰다. 아니면 닫지 않고 ``asyncio.run`` 을 끝낸다.

판정은 하지 않는다(``lab/_checks.py`` 의 ``delivery_problems``).

    python /app/lab_tools/chanlab_run.py '<spec JSON>'
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, "/app")

import django  # noqa: E402

django.setup()

from channels.layers import get_channel_layer  # noqa: E402

BASE = "http://127.0.0.1:8000/lab/chan/"
GROUP_PREFIX = "lab-g-"  # notes/chanlab.py 와 같다
PLAIN_PREFIX = "lab.plain."


def call(method: str, view: str, **params) -> tuple[int, dict]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        f"{BASE}{view}?{query}", method=method, data=b"" if method == "POST" else None
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            raise RuntimeError(f"{view} -> HTTP {exc.code}: {raw[:300]!r}") from None


async def each_worker(method: str, view: str, workers: int, deadline: float, **params) -> dict:
    """두(``workers``) PID 의 응답을 모두 모을 때까지 부른다. ``{pid: 응답}``."""
    seen: dict[int, dict] = {}
    end = time.monotonic() + deadline
    while len(seen) < workers:
        if time.monotonic() > end:
            raise TimeoutError(f"{view}: saw pids {sorted(seen)} of {workers} in {deadline}s")
        _, body = await asyncio.to_thread(call, method, view, **params)
        seen[body["pid"]] = body
    return seen


async def ready(layer, spec, key, channels, workers) -> dict:
    """probe 를 보내며 모든 워커가 probe 를 받을 때까지 기다린다(``ready_deadline``, 기본 30초)."""
    t0 = time.monotonic()
    sent = 0
    end = t0 + spec.get("ready_deadline", 30)
    while True:
        await probe(layer, spec, key, channels)
        sent += 1
        states = await each_worker("GET", "state", workers, 30, key=key)
        if all(s["probes"] > 0 for s in states.values()):
            return {"ready": True, "ready_s": round(time.monotonic() - t0, 3), "probes_sent": sent}
        if time.monotonic() > end:
            if spec.get("may_not_ready"):
                return {"ready": False, "ready_s": None, "probes_sent": sent}
            raise TimeoutError(f"not ready: {[(p, s['probes']) for p, s in states.items()]}")
        await asyncio.sleep(0.05)


async def probe(layer, spec, key, channels) -> None:
    message = {"type": "lab.probe"}
    if spec["kind"] == "group":
        await layer.group_send(GROUP_PREFIX + key, message)
    elif spec["kind"] == "plain":
        await layer.send(PLAIN_PREFIX + key, message)
    else:
        for channel in channels.values():
            await layer.send(channel, message)


async def send_from_process(layer, spec, key, channels) -> dict:
    errors = []
    pace = spec.get("pace", 0)
    targets = list(channels.values())
    t0 = time.monotonic()
    for i in range(spec["n"]):
        message = {"type": "lab.msg", "i": i}
        try:
            if spec["kind"] == "group":
                await layer.group_send(GROUP_PREFIX + key, message)
            elif spec["kind"] == "plain":
                await layer.send(PLAIN_PREFIX + key, message)
            else:
                for channel in targets:
                    await layer.send(channel, message)
        except Exception as exc:  # 모든 예외를 종류째 남긴다(ChannelFull 로 뭉뚱그리지 않는다)
            errors.append(f"{i} {type(exc).__name__}: {exc}"[:300])
        if pace:
            await asyncio.sleep(pace)
    return {"t0": t0, "t1": time.monotonic(), "errors": errors, "senders": [os.getpid()]}


async def send_from_worker(spec, key, channels) -> dict:
    """워커가 보낸다. 1:1 이면 수신 워커가 아닌 워커가 그 채널로 보낸다(``avoid_pid``)."""
    common = {"n": spec["n"], "pace": spec.get("pace", 0)}
    results = []
    if spec["kind"] == "group":
        jobs = [({"to": "group:" + GROUP_PREFIX + key}, None)]
    else:
        jobs = [({"to": ch, "avoid_pid": pid}, pid) for pid, ch in channels.items()]
    for params, _ in jobs:
        for _ in range(200):
            status, body = await asyncio.to_thread(call, "POST", "send", **params, **common)
            if status != 409:
                break
        else:
            raise TimeoutError(f"no other worker took send {params}")
        results.append({**body, "to": params["to"]})
    return {
        "t0": min(r["t0"] for r in results),
        "t1": max(r["t1"] for r in results),
        "errors": [e for r in results for e in r["errors"]],
        "senders": [r["pid"] for r in results],
        "jobs": [{"to": r["to"], "sender": r["pid"]} for r in results],
    }


async def settle(key, workers, quiet: float = 1.0, deadline: float = 20) -> dict:
    """모든 워커의 수신 수가 ``quiet`` 초 동안 그대로일 때까지 기다린다."""
    end = time.monotonic() + deadline
    last, since = None, time.monotonic()
    while True:
        states = await each_worker("GET", "state", workers, 30, key=key)
        counts = {pid: len(s["ids"]) for pid, s in states.items()}
        if counts != last:
            last, since = counts, time.monotonic()
        elif time.monotonic() - since >= quiet or time.monotonic() > end:
            return states
        await asyncio.sleep(0.1)


async def trial(layer, spec, index: int) -> dict:
    workers = spec.get("workers", 2)
    key = f"{spec['id'].lower().replace('-', '')}{index}x{uuid.uuid4().hex[:8]}"
    params = {"key": key, "kind": spec["kind"], "mode": spec.get("mode", "steady")}
    params["timeout"] = spec.get("timeout")
    params["capacity"] = spec.get("capacity")
    params["layer"] = spec.get("layer")
    started = await each_worker("POST", "listen", workers, 60, **params)
    channels = {pid: s["channel"] for pid, s in started.items()}
    readiness = await ready(layer, spec, key, channels, workers)
    if not readiness["ready"]:
        # 준비되지 않은 채 끝나도 되는 시나리오(알려진 결함). 본 메시지는 보내지 않는다.
        states = await each_worker("POST", "stop", workers, 60, key=key)
        now = time.monotonic()
        return {"key": key, **readiness, "t0": now, "t1": now, "errors": [], "senders": [],
                "workers": list(states.values())}  # fmt: skip
    if spec.get("sender", "process") == "worker":
        sent = await send_from_worker(spec, key, channels)
    else:
        sent = await send_from_process(layer, spec, key, channels)
    await settle(key, workers)
    # 용량 드롭의 합계는 자리가 난 뒤 다음 메시지가 들어올 때 "has room again after dropping N"
    # 으로만 나온다(channels-nats _enqueue). 비운 뒤 probe 를 한 번 더 보내 그 줄을 남기게 한다.
    for _ in range(10 if spec["kind"] == "plain" else 1):
        await probe(layer, spec, key, channels)
    states = await settle(key, workers)
    if not spec.get("keep"):
        states = await each_worker("POST", "stop", workers, 60, key=key)
    return {"key": key, **readiness, **sent, "workers": list(states.values())}


async def main(spec: dict) -> dict:
    layer = get_channel_layer()
    out = {"spec": spec, "trials": []}
    for index in range(spec.get("trials", 1)):
        out["trials"].append(await trial(layer, spec, index))
    if spec.get("close", True):
        t0 = time.monotonic()
        await layer.close()
        out["close_s"] = round(time.monotonic() - t0, 3)
    return out


if __name__ == "__main__":
    t0 = time.monotonic()
    result = asyncio.run(main(json.loads(sys.argv[1])))
    result["run_s"] = round(time.monotonic() - t0, 3)
    print(json.dumps(result))
