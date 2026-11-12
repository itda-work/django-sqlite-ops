"""#106 감독 변형: 앱(uvicorn)과 ``litestream replicate`` 를 따로 띄운다(Litestream 만 멈춘다).

배포 프로필은 ``litestream replicate -exec "<앱>"`` 이라 Litestream 이 끝나면 앱도 끝난다
(Litestream 이 PID 1). 그래서 '앱은 계속 쓰는데 Litestream 은 없는' 구간(상류 #1540 코멘트의
재기동 사례, #106 Q1 재시작·Q3)은 프로필 그대로는 만들 수 없다. 이 프로세스는 boot 뒤의
명령으로 돈다::

    python -m django_sqlite_ops.boot --db ... --config ... -- python /app/lab_tools/walcp_sup.py

- Litestream 을 먼저 띄우고(프로필과 같은 순서) ``migrate`` → ``uvicorn`` 을 띄운다. 명령줄은
  프로필의 ``replicate -config /etc/litestream.yml`` 과 같다(``-exec`` 만 없다).
- Litestream 이 끝나면 ``/tmp/ls.hold`` 가 없을 때 다시 띄운다. 멈추고 띄우는 것은
  ``lab_tools/lsctl.py`` 가 한다(hold 파일을 만들고 신호를 보낸다 / hold 파일을 지운다).
- SIGTERM 을 받으면 두 자식에게 SIGTERM 을 보내고 기다린다.

Litestream 의 기동·종료 시각과 rc 를 ``[sup]`` 줄로 남긴다.
"""

import os
import signal
import subprocess
import sys
import time

HOLD = "/tmp/ls.hold"
LS = ["litestream", "replicate", "-config", "/etc/litestream.yml"]
APP = [
    "sh",
    "-c",
    "python manage.py migrate --noinput && exec uvicorn proj.asgi:application "
    "--host 0.0.0.0 --port 8000 --workers 1",
]


def say(msg: str) -> None:
    print(f"[sup] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def main() -> int:
    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    ls = subprocess.Popen(LS)
    say(f"litestream started pid={ls.pid}")
    app = subprocess.Popen(APP)
    say(f"app started pid={app.pid}")
    while not stopping:
        if app.poll() is not None:
            say(f"app exited rc={app.returncode}")
            break
        if ls is not None and ls.poll() is not None:
            say(f"litestream exited rc={ls.returncode}")
            ls = None
        if ls is None and not os.path.exists(HOLD):
            ls = subprocess.Popen(LS)
            say(f"litestream started pid={ls.pid}")
        time.sleep(0.1)
    for p in (app, ls):
        if p is not None and p.poll() is None:
            p.send_signal(signal.SIGTERM)
    for p in (app, ls):
        if p is not None:
            try:
                p.wait(timeout=25)
            except subprocess.TimeoutExpired:
                p.kill()
    return app.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
