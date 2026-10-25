"""채널 레이어 전달 검증(#66)용 비동기 뷰. P2(channels-nats 를 넣은 이미지)에서만 부른다.

uvicorn 워커마다 수신 루프 하나를 띄우고(``listen``), 받은 것을 돌려준다(``state``·``stop``).
요청은 아무 워커에나 가므로 부르는 쪽(``lab_tools/chanlab_run.py``)이 두 PID 를 모두 볼 때까지
되풀이한다. 수신 루프는 워커의 이벤트 루프(uvicorn 의 루프)에서 돈다 — 실제 컨슈머와 같은
자리다.

- ``kind``: ``group``(``new_channel`` + ``group_add``), ``specific``(``new_channel`` 1:1),
  ``plain``(이름 있는 일반 채널, 두 워커가 같은 이름을 읽는다 — 큐 그룹)
- ``mode``: ``steady``(계속 ``receive``), ``cancel``(``wait_for(receive, timeout)`` 반복 — 타임아웃
  마다 취소)
- ``capacity``: 주면 그 값으로 만든 별도 레이어 인스턴스를 쓴다(D2 재현의 ``cap1000``). 문서
  설정의 레이어(``get_channel_layer()``)는 그대로다.
- ``layer``: 이름을 주면 그 이름의 별도 레이어 인스턴스(자기 NATS 연결)를 쓴다. 같은 이름의
  수신끼리 그 인스턴스를 나눈다. 알려진 결함(1ms 폴링이 연결의 읽기 루프를 끝냄,
  ``docs/research/handoff-marco-nats-flush-cancel.md``)을 다른 시나리오와 떼어 재는 데 쓴다.

``channels_nats``·``nats`` 로거의 경고·오류(용량 드롭, nats-py 읽기 루프 예외 등)를 프로세스
안에 모아 ``state`` 로 돌려준다. 예외가 붙은 기록은 ``[예외 종류: 메시지]`` 를 덧붙인다.
channels 는 함수 안에서 불러온다: P1 이미지에는 없다.
"""

import asyncio
import logging
import os
import time

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

GROUP_PREFIX = "lab-g-"
PLAIN_PREFIX = "lab.plain."
MAX_CANCEL_TIMES = 5000

_listeners: dict[str, "_Listener"] = {}
_layers: dict[tuple, object] = {}
_log: list[tuple[float, str]] = []


class _Capture(logging.Handler):
    def emit(self, record):
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            message += f" [{type(exc).__name__}: {exc}]"
        _log.append((time.monotonic(), message))


def _capture_logs() -> None:
    for name in ("channels_nats", "nats"):
        logger = logging.getLogger(name)
        if not any(isinstance(h, _Capture) for h in logger.handlers):
            logger.addHandler(_Capture(logging.WARNING))


def _layer(capacity: int | None, name: str | None = None):
    from channels.layers import get_channel_layer

    if capacity is None and name is None:
        return get_channel_layer()
    key = (capacity, name)
    if key not in _layers:
        from channels_nats import NatsChannelLayer

        config = dict(settings.CHANNEL_LAYERS["default"]["CONFIG"])
        if capacity is not None:
            config["capacity"] = capacity
        _layers[key] = NatsChannelLayer(**config)
    return _layers[key]


class _Listener:
    def __init__(
        self,
        key: str,
        kind: str,
        mode: str,
        timeout: float,
        capacity: int | None,
        layer: str | None = None,
    ):
        self.key, self.kind, self.mode, self.timeout = key, kind, mode, timeout
        self.layer = _layer(capacity, layer)
        self.capacity = capacity
        self.layer_name = layer
        self.channel = None
        self.ids: list[int] = []
        self.probes = 0
        self.cancels = 0
        self.cancel_times: list[float] = []
        self.errors: list[str] = []
        self.started = time.monotonic()
        self.stopped = None
        self.task = None

    async def start(self) -> None:
        if self.kind == "plain":
            self.channel = PLAIN_PREFIX + self.key
        else:
            self.channel = await self.layer.new_channel()
            if self.kind == "group":
                await self.layer.group_add(GROUP_PREFIX + self.key, self.channel)
        self.task = asyncio.create_task(self._run())

    async def _receive(self):
        if self.mode == "cancel":
            return await asyncio.wait_for(self.layer.receive(self.channel), self.timeout)
        return await self.layer.receive(self.channel)

    async def _run(self) -> None:
        while True:
            try:
                message = await self._receive()
            except TimeoutError:
                self.cancels += 1
                if len(self.cancel_times) < MAX_CANCEL_TIMES:
                    self.cancel_times.append(time.monotonic())
                continue
            except Exception as exc:  # 기록하고 계속 받는다(단언은 errors 가 비었는지로)
                self.errors.append(f"{type(exc).__name__}: {exc}"[:300])
                await asyncio.sleep(0.1)
                continue
            if message.get("type") == "lab.probe":
                self.probes += 1
            else:
                self.ids.append(message["i"])

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.stopped is None:
            self.stopped = time.monotonic()

    def info(self) -> dict:
        until = self.stopped or time.monotonic()
        return {
            "pid": os.getpid(),
            "key": self.key,
            "kind": self.kind,
            "mode": self.mode,
            "capacity": self.capacity,
            "layer": self.layer_name,
            "channel": self.channel,
            "ids": self.ids,
            "probes": self.probes,
            "cancels": self.cancels,
            "cancel_times": self.cancel_times,
            "errors": self.errors,
            "running": self.task is not None and not self.task.done(),
            "nats_log": [m for t, m in _log if self.started <= t <= until],
            "now": time.monotonic(),
        }


def _missing(key: str) -> JsonResponse:
    return JsonResponse({"pid": os.getpid(), "key": key, "missing": True})


@csrf_exempt
async def listen(request):
    """POST: 이 워커에 수신 루프를 띄운다. 이미 있으면 그 상태를 돌려준다."""
    _capture_logs()
    key = request.GET["key"]
    listener = _listeners.get(key)
    if listener is None:
        capacity = request.GET.get("capacity")
        listener = _Listener(
            key,
            request.GET["kind"],
            request.GET.get("mode", "steady"),
            float(request.GET.get("timeout", "0.5")),
            int(capacity) if capacity else None,
            request.GET.get("layer") or None,
        )
        _listeners[key] = listener
        await listener.start()
    return JsonResponse(listener.info())


async def state(request):
    key = request.GET["key"]
    listener = _listeners.get(key)
    return JsonResponse(listener.info()) if listener else _missing(key)


@csrf_exempt
async def stop(request):
    """POST: 수신 루프를 취소하고 마지막 상태를 돌려준다."""
    key = request.GET["key"]
    listener = _listeners.get(key)
    if listener is None:
        return _missing(key)
    await listener.stop()
    return JsonResponse(listener.info())


@csrf_exempt
async def send(request):
    """POST: 이 워커에서 보낸다(워커 → 워커). ``avoid_pid`` 가 이 워커면 409 로 보내지 않는다.

    ``to`` 가 ``group:<이름>`` 이면 ``group_send``, 아니면 그 채널로 ``send``. ``i`` 는 0..n-1.
    """
    if request.GET.get("avoid_pid") == str(os.getpid()):
        return JsonResponse({"pid": os.getpid(), "avoided": True}, status=409)
    layer = _layer(None)
    to = request.GET["to"]
    n = int(request.GET["n"])
    pace = float(request.GET.get("pace", "0"))
    errors = []
    t0 = time.monotonic()
    for i in range(n):
        message = {"type": "lab.msg", "i": i}
        try:
            if to.startswith("group:"):
                await layer.group_send(to.removeprefix("group:"), message)
            else:
                await layer.send(to, message)
        except Exception as exc:
            errors.append(f"{i} {type(exc).__name__}: {exc}"[:300])
        if pace:
            await asyncio.sleep(pace)
    return JsonResponse(
        {"pid": os.getpid(), "sent": n, "errors": errors, "t0": t0, "t1": time.monotonic()}
    )


async def tasks(request):
    """진단: 이 워커 이벤트 루프의 태스크와 각 스택 맨 위 몇 줄(멈춘 곳을 보려고)."""
    import io

    out = []
    for task in asyncio.all_tasks():
        buf = io.StringIO()
        task.print_stack(limit=8, file=buf)
        out.append(
            {
                "name": task.get_name(),
                "coro": repr(task.get_coro())[:200],
                "stack": buf.getvalue()[-1500:],
            }
        )
    return JsonResponse({"pid": os.getpid(), "tasks": out})
