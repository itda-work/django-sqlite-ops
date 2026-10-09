"""벤치 계측(#26). 랩 앱 프로세스 안의 카운터와 DB 파일 상태를 읽는다.

- ``connection_created`` 시그널 횟수와 DB 를 쓰는 요청 수: ``CONN_MAX_AGE`` 가 실제로 연결을
  재사용하는지 서버에서 센다(연결 수 / 요청 수가 1 에 가까우면 재사용하지 않는다).
- ``-wal`` 헤더의 체크포인트 순번(``ckpt_seq``)과 salt 두 개: 날것 그대로 돌려준다. ``ckpt_seq`` 는
  헤더를 쓴 연결 핸들의 카운터라 여러 연결이 WAL 을 재시작하면 재시작 횟수도 그 하한도 아니다
  (#26 리뷰 1·2, 재현함). salt 는 WAL 헤더를 다시 쓸 때마다 바뀌므로, 두 표본 사이에 salt 가
  바뀌었으면 그 사이 헤더가 한 번 이상 다시 쓰였다는 것만 말할 수 있다
  (sqlite.org/fileformat.html#walformat).

DB 연결을 열지 않는다(``/lab/probe`` 가 연결 수를 늘리지 않게).
"""

import os
import resource
import struct
import threading
import time

from django.conf import settings

_lock = threading.Lock()
_counts = {"conn_created": 0, "db_requests": 0}


def bump(key: str) -> None:
    with _lock:
        _counts[key] += 1


def on_connection_created(sender, connection, **kwargs) -> None:
    bump("conn_created")


def db_fds(path: str) -> int | None:
    """이 프로세스가 연 DB 파일(본체·``-wal``·``-shm``) 디스크립터 수. Linux 에서만."""
    try:
        names = os.listdir("/proc/self/fd")
    except FileNotFoundError:
        return None
    n = 0
    for name in names:
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target in (path, path + "-wal", path + "-shm"):
            n += 1
    return n


def wal_header(path: str) -> dict | None:
    try:
        with open(path + "-wal", "rb") as f:
            head = f.read(32)
    except FileNotFoundError:
        return None
    if len(head) < 32:
        return None
    _magic, _version, page_size, ckpt_seq, salt1, salt2 = struct.unpack(">6I", head[:24])
    return {"page_size": page_size, "ckpt_seq": ckpt_seq, "salt1": salt1, "salt2": salt2}


def snapshot() -> dict:
    name = settings.DATABASES["default"]["NAME"]
    sizes = {}
    for suffix in ("", "-wal", "-shm"):
        try:
            sizes[f"db{suffix}"] = os.stat(name + suffix).st_size
        except FileNotFoundError:
            sizes[f"db{suffix}"] = None
    with _lock:
        counts = dict(_counts)
    return {
        "t": time.monotonic(),
        "cpu_s": time.process_time(),
        "threads": threading.active_count(),
        "db_fds": db_fds(name),
        "fd_limit": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
        "sizes": sizes,
        "wal": wal_header(name),
        **counts,
    }
