import os
import time

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")

django_application = get_asgi_application()


async def application(scope, receive, send):
    """벤치 계측(#26). Django 앞에서 두 가지만 더한다.

    - ``/lab/rawping``: Django 를 거치지 않고 바로 답한다(uvicorn·부하 발생기의 한계 확인).
    - 응답 헤더 ``X-Lab-App-Us``: 요청을 받은 때부터 응답 시작까지(스레드 배정·대기 포함,
      마이크로초).
    """
    if scope["type"] != "http":
        return await django_application(scope, receive, send)
    if scope["path"] == "/lab/rawping":
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", b"11")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})
        return
    t0 = time.perf_counter()

    async def timed_send(message):
        if message["type"] == "http.response.start":
            us = str(int((time.perf_counter() - t0) * 1e6)).encode()
            message = {**message, "headers": [*message.get("headers", []), (b"x-lab-app-us", us)]}
        await send(message)

    await django_application(scope, receive, timed_send)
