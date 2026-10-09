"""벤치 계측(#26): 뷰 본문 시간을 응답 헤더 ``X-Lab-View-Us``(마이크로초)로 돌려준다.

``urls_tail.py`` 가 랩 뷰를 이 래퍼로 감싼다. 잰 시간은 동기 뷰 함수 호출 하나(뷰·ORM·SQLite
작업)다. 스레드 배정·이벤트 루프 왕복은 들어가지 않지만, 같은 프로세스의 다른 스레드와 GIL 을
다투며 기다린 시간은 들어간다(구분할 수 없다). 요청을 받은 때부터 응답 시작까지는
``proj/asgi.py`` 의 ``X-Lab-App-Us`` 다.

#26 첫 실행은 동기 미들웨어(``X-Lab-Svc-Us``)로 쟀다. ASGI 핸들러에서 sync-only 미들웨어는
``async_to_sync`` 로 감싸이고 그 안에서 뷰를 다시 ``sync_to_async`` 로 부르므로, 그 값에는 이벤트
루프 왕복과 뷰 스레드 배정 대기가 들어 있었고 배포 프로필에 없는 왕복도 더했다(#26 리뷰 1, 재현함).
그래서 미들웨어를 쓰지 않는다.
"""

import functools
import time

from . import metrics


def timed(view, *, db: bool = True):
    """``db`` 가 참이면 연결 재사용 비율의 분모(DB 요청 수)에 센다.

    예외(``OperationalError``, ``Http404`` 등)로 끝난 요청도 ``finally`` 에서 한 번 센다. 연결 생성
    시그널은 SQL 실패 전에 이미 났을 수 있으므로, 빼면 비율이 1 을 넘는다(#26 리뷰 2, 재현함).
    예외는 그대로 전파한다(Django 가 500·404 로 바꾼다).
    """

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            response = view(request, *args, **kwargs)
            response["X-Lab-View-Us"] = str(int((time.perf_counter() - t0) * 1e6))
            return response
        finally:
            if db:
                metrics.bump("db_requests")

    return wrapper
