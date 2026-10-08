"""벤치 계측(#26): 뷰 처리 시간을 응답 헤더 ``X-Lab-Svc-Us``(마이크로초)로 돌려준다.

동기 미들웨어라 ASGI 에서 요청 스레드 안에서 돈다. 잰 시간은 뷰·ORM·SQLite 작업이고 스레드
배정·이벤트 루프 대기는 들어가지 않는다(그것까지 포함한 시간은 ``proj/asgi.py`` 의
``X-Lab-App-Us``).
"""

import time

from . import metrics

# DB 를 열지 않는 경로. 연결 재사용 비율의 분모(DB 요청 수)에서 뺀다.
NO_DB = ("/lab/probe", "/lab/ping")


def timing(get_response):
    def middleware(request):
        t0 = time.perf_counter()
        response = get_response(request)
        response["X-Lab-Svc-Us"] = str(int((time.perf_counter() - t0) * 1e6))
        if not request.path.startswith(NO_DB):
            metrics.bump("db_requests")
        return response

    return middleware
