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


# --- soak(#33): fd 분류·RSS·스레드·cgroup 메모리 ------------------------------------------


def fd_breakdown(path: str, fd_dir: str = "/proc/self/fd") -> dict | None:
    """열린 디스크립터를 DB 본체·``-wal``·``-shm``·소켓·기타로 나눈다(``readlink``). Linux 에서만.

    fd 가 수십만 개면 이 열거가 앱 프로세스에서 눈에 띄는 시간이 걸린다(soak 표본 비용).
    걸린 시간을 ``scan_ms`` 로 같이 돌려준다.
    """
    t0 = time.perf_counter()
    try:
        names = os.listdir(fd_dir)
    except FileNotFoundError:
        return None
    out = {"db": 0, "wal": 0, "shm": 0, "socket": 0, "other": 0}
    for name in names:
        try:
            target = os.readlink(f"{fd_dir}/{name}")
        except OSError:  # 열거와 readlink 사이에 닫힌 fd(listdir 자신의 fd 포함)
            continue
        out[classify_fd(target, path)] += 1
    out["total"] = sum(out.values())
    out["scan_ms"] = round(1000 * (time.perf_counter() - t0), 1)
    return out


def classify_fd(target: str, path: str) -> str:
    if target == path:
        return "db"
    if target == path + "-wal":
        return "wal"
    if target == path + "-shm":
        return "shm"
    if target.startswith("socket:"):
        return "socket"
    return "other"


def proc_status(status_path: str = "/proc/self/status") -> dict:
    """``VmRSS``(바이트)·``VmHWM``(최대 RSS)·``Threads``(OS 스레드)."""
    out: dict = {}
    try:
        with open(status_path, encoding="ascii") as f:
            text = f.read()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "VmHWM"):
            out[key] = int(value.split()[0]) * 1024  # kB
        elif key == "Threads":
            out[key] = int(value)
    return out


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as f:
            raw = f.read().strip()
    except (FileNotFoundError, PermissionError):
        return None
    return None if raw == "max" else int(raw)


def cgroup_memory() -> dict:
    """컨테이너 cgroup(v2)의 메모리 사용량·한도·OOM 이벤트. 커널 메모리(열린 파일 등)도 들어간다."""
    out = {
        "current": _read_int("/sys/fs/cgroup/memory.current"),
        "max": _read_int("/sys/fs/cgroup/memory.max"),
        "swap_max": _read_int("/sys/fs/cgroup/memory.swap.max"),
    }
    try:
        with open("/sys/fs/cgroup/memory.events", encoding="ascii") as f:
            for line in f:
                key, value = line.split()
                if key in ("oom", "oom_kill", "max"):
                    out[f"events_{key}"] = int(value)
    except FileNotFoundError:
        pass
    return out


def soak_snapshot() -> dict:
    """soak 표본(#33). DB 연결을 열지 않는다."""
    import gc

    name = settings.DATABASES["default"]["NAME"]
    try:
        wal = os.stat(name + "-wal").st_size
    except FileNotFoundError:
        wal = None
    with _lock:
        counts = dict(_counts)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {
        "t": time.monotonic(),
        "pid": os.getpid(),
        "cpu_s": time.process_time(),
        "fds": fd_breakdown(name),
        "fd_limit": [soft, hard],
        "py_threads": threading.active_count(),
        **proc_status(),
        "cgroup": cgroup_memory(),
        "gc_count": list(gc.get_count()),
        "gc_collections": [s["collections"] for s in gc.get_stats()],
        "wal_bytes": wal,
        **counts,
    }


def soak_env() -> dict:
    """soak 실행 환경(#33): 커널 상한과 이 프로세스·PID 1 에 실제 적용된 한도(원문)."""

    def read(p: str) -> str | None:
        try:
            with open(p, encoding="ascii") as f:
                return f.read()
        except (FileNotFoundError, PermissionError):
            return None

    return {
        "pid": os.getpid(),
        "nr_open": read("/proc/sys/fs/nr_open"),
        "file_max": read("/proc/sys/fs/file-max"),
        "file_nr": read("/proc/sys/fs/file-nr"),
        "self_limits": read("/proc/self/limits"),
        "pid1_limits": read("/proc/1/limits"),
        "pid1_cmdline": (read("/proc/1/cmdline") or "").replace("\0", " ").strip(),
        "cgroup": cgroup_memory(),
        "conn_max_age": settings.DATABASES["default"].get("CONN_MAX_AGE", 0),
    }
