"""#106: 앱 컨테이너 안의 ``litestream replicate`` 를 멈추거나 다시 띄운다. JSON 한 줄을 낸다.

    python lsctl.py stop [TERM|KILL]   # /tmp/ls.hold 를 만들고 신호를 보낸 뒤 끝날 때까지 기다린다
    python lsctl.py start              # /tmp/ls.hold 를 지운다(walcp_sup.py 가 다시 띄운다)
    python lsctl.py pids               # replicate 프로세스 목록

이미지에 ``ps``·``pkill`` 이 없으므로 ``/proc`` 를 읽는다. 감독 변형(``walcp_sup.py``)에서만 쓴다.
"""

import json
import os
import signal
import sys
import time

HOLD = "/tmp/ls.hold"


def replicate_pids() -> list[int]:
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            with open(f"/proc/{name}/stat") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if state == "Z":
            continue
        if argv and os.path.basename(argv[0]) == "litestream" and argv[1:2] == ["replicate"]:
            found.append(int(name))
    return sorted(found)


def main() -> None:
    action = sys.argv[1]
    if action == "pids":
        print(json.dumps({"pids": replicate_pids()}))
    elif action == "stop":
        sig = signal.SIGKILL if sys.argv[2:3] == ["KILL"] else signal.SIGTERM
        open(HOLD, "w").close()
        pids = replicate_pids()
        t0 = time.monotonic()
        for pid in pids:
            os.kill(pid, sig)
        while replicate_pids() and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        print(
            json.dumps(
                {
                    "stopped": pids,
                    "signal": sig.name,
                    "exit_s": round(time.monotonic() - t0, 2),
                    "left": replicate_pids(),
                }
            )
        )
    elif action == "start":
        if os.path.exists(HOLD):
            os.unlink(HOLD)
        t0 = time.monotonic()
        while not replicate_pids() and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        print(json.dumps({"pids": replicate_pids()}))
    else:
        raise SystemExit(f"unknown action {action}")


if __name__ == "__main__":
    main()
