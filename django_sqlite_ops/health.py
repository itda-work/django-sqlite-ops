"""복제 헬스 (DESIGN §7).

별칭마다 로컬 메타의 최대 TXID 와 복제본(``litestream ltx``)의 최대 TXID 를 직접 비교한다.
Litestream 은 S3 가 끊겨도 로그·``status``·메트릭에 아무것도 남기지 않기 때문이다(D3).
**DB 연결을 열지 않는다.** 로컬 TXID 는 메타 파일에서, 원격은 ``litestream ltx`` 로 읽는다.

요청마다 S3 를 부르지 않는다. 프로세스당 데몬 스레드 하나가 ``REFRESH`` 초마다 조회하고, 뷰는
마지막 결과를 읽기만 한다. 스레드는 첫 헬스 요청 때 시작한다(``ready()`` 에서 시작하지 않는다 —
관리 명령·테스트·마이그레이션에서 스레드가 돌지 않게). PID 가 바뀌면(포크) 다시 시작한다.

상태 계산은 시각과 조회 결과를 받는 순수 함수(``next_backlog_since``, ``alias_status``,
``overall_status``)에 모은다.
"""

import json
import math
import os
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import HttpRequest, JsonResponse

from .boot import litestream
from .boot.cli import real_path, state_path
from .boot.decide import Remote, RemoteEmpty, RemoteError, RemoteTxid
from .database import ENGINE

__all__ = [
    "BACKLOG",
    "CAUGHT_UP",
    "DEFAULT_BACKLOG_GRACE",
    "DEFAULT_REFRESH",
    "SCHEMA_VERSION",
    "STALE_FACTOR",
    "UNKNOWN",
    "AliasConfig",
    "HealthConfig",
    "Monitor",
    "Sample",
    "alias_status",
    "get_monitor",
    "health_view",
    "next_backlog_since",
    "overall_status",
    "parse_config",
    "read_boot_state",
]

SCHEMA_VERSION = 1
CAUGHT_UP = "caught_up"
BACKLOG = "backlog"
UNKNOWN = "unknown"
# 전체 상태는 가장 나쁜 별칭을 따른다. unknown 이 가장 나쁘다: 복제가 따라오는지 말할 수 없다는
# 뜻이라 backlog(뒤처졌지만 따라오는 중임을 안다)보다 더 많은 것을 감출 수 있기 때문이다.
_SEVERITY = {CAUGHT_UP: 0, BACKLOG: 1, UNKNOWN: 2}

DEFAULT_REFRESH = 15.0
DEFAULT_BACKLOG_GRACE = 60.0
# 마지막 결과가 REFRESH × 3 보다 오래됐으면 스레드가 멈춘 것으로 보고 unknown 이다.
STALE_FACTOR = 3

_HEALTH_KEYS = frozenset({"DATABASES", "REFRESH", "BACKLOG_GRACE"})
_ALIAS_KEYS = frozenset({"litestream_config", "meta_path", "litestream"})
# 부팅 상태 파일(DESIGN §7)에서 응답에 옮기는 키. 그 밖의 키는 옮기지 않는다.
_BOOT_KEYS = (
    "state",
    "action",
    "reason_code",
    "reason",
    "unknown_at_boot",
    "litestream_version",
    "at",
)
_BOOT_MAX_BYTES = 64 * 1024
_MAX_REASON = 300


def _one_line(text: object) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= _MAX_REASON else line[: _MAX_REASON - 1] + "…"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex(txid: int | None) -> str | None:
    return None if txid is None else f"{txid:016x}"


# --- 설정 ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AliasConfig:
    alias: str
    litestream_config: str
    meta_path: str | None = None
    litestream: str = "litestream"


@dataclass(frozen=True, slots=True)
class HealthConfig:
    databases: tuple[AliasConfig, ...]
    refresh: float = DEFAULT_REFRESH
    backlog_grace: float = DEFAULT_BACKLOG_GRACE


def _path_text(value: Any) -> str | None:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    return value


def _number(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _binary(binary: str) -> str:
    """경로 구분자가 든 바이너리는 절대 경로로, PATH 로 찾는 이름은 그대로 둔다."""
    if os.sep in binary or (os.altsep and os.altsep in binary):
        return os.path.abspath(binary)
    return binary


def parse_config(
    raw: Any, databases: Mapping[str, Any]
) -> tuple[HealthConfig | None, list[tuple[str, str | None]]]:
    """``SQLITE_OPS["HEALTH"]`` 를 검증한다. ``(설정 또는 None, [(메시지, 별칭|None)])``.

    오류가 하나라도 있으면 설정은 ``None`` 이다. 시스템 체크 E002 가 같은 오류를 보고한다.
    상대 경로는 이 프로세스의 작업 디렉터리 기준 절대 경로로 바꾼다.
    """
    errors: list[tuple[str, str | None]] = []
    if not isinstance(raw, Mapping):
        return None, [(f"SQLITE_OPS['HEALTH'] must be a dict, got {type(raw).__name__}.", None)]
    for key in sorted(set(map(str, raw)) - _HEALTH_KEYS):
        errors.append((f"Unknown key {key!r} in SQLITE_OPS['HEALTH'].", None))

    refresh = _number(raw.get("REFRESH", DEFAULT_REFRESH))
    if refresh is None or refresh <= 0:
        errors.append(
            (
                f"SQLITE_OPS['HEALTH']['REFRESH'] must be a positive number of seconds, "
                f"got {raw.get('REFRESH')!r}.",
                None,
            )
        )
    grace = _number(raw.get("BACKLOG_GRACE", DEFAULT_BACKLOG_GRACE))
    if grace is None or grace < 0:
        errors.append(
            (
                f"SQLITE_OPS['HEALTH']['BACKLOG_GRACE'] must be a non-negative number of seconds, "
                f"got {raw.get('BACKLOG_GRACE')!r}.",
                None,
            )
        )

    entries = raw.get("DATABASES")
    aliases: list[AliasConfig] = []
    if not isinstance(entries, Mapping) or not entries:
        errors.append(("SQLITE_OPS['HEALTH']['DATABASES'] must be a non-empty dict.", None))
        entries = {}
    for alias, entry in entries.items():
        where = f"SQLITE_OPS['HEALTH']['DATABASES'][{alias!r}]"
        if not isinstance(alias, str):
            errors.append((f"{where}: the alias must be a string.", None))
            continue
        db = databases.get(alias)
        if not isinstance(db, Mapping):
            errors.append((f"{where}: {alias!r} is not in DATABASES.", alias))
        elif db.get("ENGINE") != ENGINE:
            errors.append((f"{where}: {alias!r} is not a {ENGINE} database.", alias))
        if not isinstance(entry, Mapping):
            errors.append((f"{where} must be a dict, got {type(entry).__name__}.", alias))
            continue
        for key in sorted(set(map(str, entry)) - _ALIAS_KEYS):
            errors.append((f"{where}: unknown key {key!r}.", alias))
        config = _path_text(entry.get("litestream_config"))
        if config is None:
            errors.append((f"{where}['litestream_config'] must be a non-empty path.", alias))
        meta = entry.get("meta_path")
        if meta is not None and _path_text(meta) is None:
            errors.append((f"{where}['meta_path'] must be a non-empty path.", alias))
        binary = entry.get("litestream", "litestream")
        if _path_text(binary) is None:
            errors.append((f"{where}['litestream'] must be a non-empty string.", alias))
        if errors:
            continue
        meta_text = _path_text(meta)
        aliases.append(
            AliasConfig(
                alias,
                os.path.abspath(config),
                os.path.abspath(meta_text) if meta_text is not None else None,
                _binary(_path_text(binary)),
            )
        )
    if errors:
        return None, errors
    return HealthConfig(tuple(aliases), refresh, grace), []


def load_config() -> tuple[HealthConfig | None, list[tuple[str, str | None]]]:
    """설정에서 읽는다. ``HEALTH`` 가 없으면 ``(None, [])``."""
    ops = getattr(settings, "SQLITE_OPS", {})
    if not isinstance(ops, Mapping):
        return None, [("SQLITE_OPS must be a dict.", None)]
    if "HEALTH" not in ops:
        return None, []
    return parse_config(ops["HEALTH"], settings.DATABASES)


# --- 조회 결과 -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sample:
    """한 번의 갱신에서 본 별칭 하나의 사실. 판정은 ``alias_status()`` 가 한다.

    ``checked_at`` 은 UNIX 초(벽시계). ``error`` 는 조회 전에 판정이 끝난 사유(경로 규칙 위반,
    파일 DB 가 아님, 갱신 중 예외)이고, 있으면 다른 필드와 무관하게 ``unknown`` 이다.
    """

    checked_at: float
    path: str | None = None
    local: int | None = None
    remote: Remote | None = None
    boot_state: dict[str, Any] | None = None
    boot_state_error: str | None = None
    error: str | None = None
    backlog_since: float | None = None


def read_boot_state(db_path: str) -> tuple[dict[str, Any] | None, str | None]:
    """``<db>.boot-state.json`` 을 읽는다. ``(내용, 문제)`` 중 하나만 값이 있다.

    파일이 없거나 형식이 틀리면 그 사실만 돌려준다(boot 를 쓰지 않는 배포도 있다).
    """
    path = state_path(Path(os.path.abspath(db_path)))
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "no boot state file (boot was not used, or has not run yet)"
    except OSError as exc:
        return None, _one_line(f"cannot open boot state file: {exc.strerror or exc}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "boot state file is not a regular file"
        data = os.read(fd, _BOOT_MAX_BYTES + 1)
    except OSError as exc:
        return None, _one_line(f"cannot read boot state file: {exc.strerror or exc}")
    finally:
        os.close(fd)
    if len(data) > _BOOT_MAX_BYTES:
        return None, "boot state file is too large"
    try:
        body = json.loads(data)
    except ValueError:
        return None, "boot state file is not valid JSON"
    if type(body) is not dict or body.get("version") != 1:
        return None, "boot state file has an unexpected format (version 1 expected)"
    if type(body.get("unknown_at_boot")) is not bool:
        return None, "boot state file has an unexpected format (unknown_at_boot)"
    for key in _BOOT_KEYS:
        value = body.get(key)
        if value is not None and type(value) not in (str, bool):
            return None, f"boot state file has an unexpected format ({key})"
    return {key: body.get(key) for key in _BOOT_KEYS}, None


# --- 판정 (순수 함수) ------------------------------------------------------------------


def next_backlog_since(
    prev: float | None, local: int | None, remote: Remote | None, checked_at: float
) -> float | None:
    """로컬이 원격보다 앞서기 시작한 시각. 앞서 있지 않으면 ``None``.

    앞선 상태가 이어지면 처음 본 시각을 유지하고, 끊기면 지운다. 프로세스 안에서만 추적하므로
    재시작하면 초기화된다.
    """
    if type(remote) is RemoteTxid and type(local) is int and local > remote.txid:
        return prev if prev is not None else checked_at
    return None


def _fields(sample: Sample | None, now: float) -> dict[str, Any]:
    if sample is None:
        return {
            "path": None,
            "local_txid": None,
            "remote_txid": None,
            "checked_at": None,
            "age": None,
            "backlog_since": None,
            "boot_state": None,
            "boot_state_error": None,
        }
    remote = sample.remote.txid if type(sample.remote) is RemoteTxid else None
    return {
        "path": sample.path,
        "local_txid": _hex(sample.local),
        "remote_txid": _hex(remote),
        "checked_at": _iso(sample.checked_at),
        "age": round(now - sample.checked_at, 3),
        "backlog_since": _iso(sample.backlog_since),
        "boot_state": sample.boot_state,
        "boot_state_error": sample.boot_state_error,
    }


def _verdict(sample: Sample | None, now: float, refresh: float, grace: float) -> tuple[str, str]:
    if sample is None:
        return UNKNOWN, "not checked yet; the first refresh has not finished"
    age = now - sample.checked_at
    if age < 0:
        return UNKNOWN, "the last check is in the future (the clock moved backwards)"
    if age > refresh * STALE_FACTOR:
        return UNKNOWN, (
            f"the last check is {age:.0f}s old (more than {STALE_FACTOR} x REFRESH); "
            "the refresh thread may be stuck"
        )
    if sample.error is not None:
        return UNKNOWN, sample.error
    if sample.boot_state is not None and sample.boot_state.get("unknown_at_boot") is True:
        return UNKNOWN, (
            "unknown_at_boot: boot proceeded with --on-unknown keep-local; replication state "
            "is unknown until the next boot that is not keep-local"
        )
    remote = sample.remote
    if type(remote) is RemoteError:
        return UNKNOWN, f"remote lookup failed: {remote.message}"
    if type(remote) is RemoteEmpty:
        return UNKNOWN, (
            "the replica is empty (or the replica path/prefix is wrong; litestream reports "
            "both the same way)"
        )
    if type(remote) is not RemoteTxid:
        return UNKNOWN, "no remote result"
    if sample.local is None:
        return UNKNOWN, "no readable local Litestream metadata (is litestream replicate running?)"
    if remote.txid > sample.local:
        return UNKNOWN, (
            f"the replica is ahead of local ({remote.txid:016x} > {sample.local:016x}); another "
            "machine may be writing to the same replica"
        )
    if remote.txid == sample.local:
        return CAUGHT_UP, "local and replica are at the same TXID"
    since = sample.backlog_since if sample.backlog_since is not None else sample.checked_at
    behind = sample.checked_at - since
    if behind >= grace:
        return BACKLOG, (
            f"local has been ahead of the replica for {behind:.0f}s (BACKLOG_GRACE {grace:g}s)"
        )
    return CAUGHT_UP, (
        f"local is ahead of the replica for {behind:.0f}s, within BACKLOG_GRACE {grace:g}s"
    )


def alias_status(
    sample: Sample | None, now: float, *, refresh: float, grace: float
) -> dict[str, Any]:
    """별칭 하나의 상태(응답의 ``databases[alias]``).

    판정 순서(처음 맞는 것): 조회 전 · 오래된 결과(``REFRESH × 3`` 초과) · 사전 오류 ·
    ``unknown_at_boot`` · 원격 실패 · 원격 빈 목록 · 로컬 메타 없음 · 원격이 앞섬 → ``unknown``.
    같으면 ``caught_up``. 로컬이 앞서면 ``backlog_since`` 부터 ``checked_at`` 까지가 ``grace``
    이상일 때 ``backlog``, 아니면 ``caught_up``.
    """
    status, reason = _verdict(sample, now, refresh, grace)
    return {"status": status, "reason": _one_line(reason), **_fields(sample, now)}


def overall_status(statuses: Mapping[str, Mapping[str, Any]] | list[str]) -> str:
    """가장 나쁜 상태(``unknown`` > ``backlog`` > ``caught_up``). 별칭이 없으면 ``unknown``."""
    values = [s["status"] for s in statuses.values()] if isinstance(statuses, Mapping) else statuses
    if not values:
        return UNKNOWN
    return max(values, key=lambda s: _SEVERITY.get(s, _SEVERITY[UNKNOWN]))


# --- 조회 ------------------------------------------------------------------------------


def _db_path(alias: str) -> tuple[str | None, str | None]:
    """Django 가 여는 파일 경로(연결하지 않는다). ``(경로, 문제)``."""
    from . import doctor  # doctor 는 checks 를 import 한다. 순환을 피해 늦게 읽는다.

    config = settings.DATABASES.get(alias)
    if not isinstance(config, Mapping):
        return None, f"{alias!r} is not in DATABASES"
    target = doctor._target(alias, config)
    if target.role != "write":
        why = f": {target.reason}" if target.reason else ""
        return None, f"not a writable file database (role {target.role}{why})"
    if target.path is None:
        return None, "the database path is empty"
    return target.path, None


def _check_real(path: str, what: str) -> str | None:
    """D-15: 실제 경로가 아니면 사유를 돌려준다."""
    try:
        real = real_path(path)
    except ValueError as exc:
        return _one_line(f"{what} is not a real path: {exc}")
    if os.path.islink(real):
        return f"{what} {real} is a symbolic link; use the real path (D-15)"
    return None


Probe = Callable[[AliasConfig, str], Remote]
LocalProbe = Callable[[str, str | None], int | None]


def _remote(cfg: AliasConfig, db: str) -> Remote:
    return litestream.remote_max_txid(db, config=cfg.litestream_config, binary=cfg.litestream)


def _local(db: str, meta: str | None) -> int | None:
    return litestream.local_max_txid(db, meta_path=meta)


@dataclass
class Monitor:
    """프로세스당 하나. 데몬 스레드가 ``refresh`` 초마다 ``samples`` 를 갱신한다."""

    config: HealthConfig
    remote: Probe = _remote
    local: LocalProbe = _local
    path_of: Callable[[str], tuple[str | None, str | None]] = _db_path
    clock: Callable[[], float] = time.time
    samples: dict[str, Sample] = field(default_factory=dict, init=False)
    refreshes: int = field(default=0, init=False)
    _pid: int | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # -- 갱신 --

    def _sample(self, cfg: AliasConfig, prev: Sample | None) -> Sample:
        now = self.clock()
        path, problem = self.path_of(cfg.alias)
        if problem is not None or path is None:
            return Sample(now, error=problem or "no database path")
        problem = _check_real(path, "database path")
        if problem is None and cfg.meta_path is not None:
            problem = _check_real(cfg.meta_path, "meta_path")
        boot_state, boot_error = read_boot_state(path)
        if problem is not None:
            return Sample(
                now, path=path, boot_state=boot_state, boot_state_error=boot_error, error=problem
            )
        remote = self.remote(cfg, path)
        local = self.local(path, cfg.meta_path)
        checked_at = self.clock()
        since = next_backlog_since(
            prev.backlog_since if prev is not None else None, local, remote, checked_at
        )
        return Sample(checked_at, path, local, remote, boot_state, boot_error, None, since)

    def refresh_once(self) -> None:
        """모든 별칭을 한 번 갱신한다. 예외는 삼키고 그 별칭을 ``unknown`` 사유로 남긴다."""
        for cfg in self.config.databases:
            with self._lock:
                prev = self.samples.get(cfg.alias)
            try:
                sample = self._sample(cfg, prev)
            except Exception as exc:  # noqa: BLE001 - 스레드가 죽지 않게 사유로 남긴다
                try:
                    now = self.clock()
                except Exception:  # noqa: BLE001
                    now = time.time()
                sample = Sample(
                    now, error=_one_line(f"refresh failed: {type(exc).__name__}: {exc}")
                )
            with self._lock:
                self.samples[cfg.alias] = sample
        with self._lock:
            self.refreshes += 1

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.refresh_once()
            except Exception:  # noqa: BLE001 - refresh_once 는 던지지 않지만 스레드를 지킨다
                pass
            stop.wait(self.config.refresh)

    # -- 스레드 --

    @property
    def running(self) -> bool:
        return self._thread is not None and self._pid == os.getpid() and self._thread.is_alive()

    def ensure_started(self) -> None:
        """스레드가 없거나, 다른 프로세스(포크 전 부모)의 것이면 새로 시작한다."""
        pid = os.getpid()
        if self._pid != pid:
            # 포크된 자식: 부모의 잠금·결과·스레드 객체를 물려받았다. 처음부터 다시 한다.
            self._lock = threading.Lock()
            self._stop = threading.Event()
            self.samples = {}
            self._thread = None
            self._pid = pid
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._stop,), name="sqlite-ops-health", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None and self._pid == os.getpid():
            self._thread.join(timeout)

    # -- 보고 --

    def report(self, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        with self._lock:
            samples = dict(self.samples)
        databases = {
            cfg.alias: alias_status(
                samples.get(cfg.alias),
                now,
                refresh=self.config.refresh,
                grace=self.config.backlog_grace,
            )
            for cfg in self.config.databases
        }
        return {
            "version": SCHEMA_VERSION,
            "status": overall_status(databases),
            "refresh": self.config.refresh,
            "backlog_grace": self.config.backlog_grace,
            "databases": databases,
        }


_monitor: Monitor | None = None
_monitor_lock = threading.Lock()


def _reset_lock_after_fork() -> None:
    # 포크 순간 다른 스레드가 잡고 있던 잠금을 자식이 영원히 기다리지 않게 한다.
    global _monitor_lock
    _monitor_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_lock_after_fork)


def get_monitor() -> tuple[Monitor | None, str | None]:
    """설정으로 만든 프로세스의 ``Monitor`` 를 돌려주고 스레드를 시작한다. ``(모니터, 문제)``."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            config, errors = load_config()
            if config is None:
                if errors:
                    return None, _one_line(
                        "invalid SQLITE_OPS['HEALTH'] (see manage.py check, sqlite_ops.E002): "
                        + errors[0][0]
                    )
                return None, "SQLITE_OPS['HEALTH'] is not configured"
            _monitor = Monitor(config)
        _monitor.ensure_started()
        return _monitor, None


# --- 뷰 --------------------------------------------------------------------------------

_STRICT_TRUE = frozenset({"1", "true", "yes", "on"})


def health_view(request: HttpRequest) -> JsonResponse:
    """복제 헬스 JSON. HTTP 상태는 항상 200 이다(``?strict=1`` 이면 ``caught_up`` 이 아닐 때 503).

    복제가 뒤처졌다고 로드밸런서가 앱을 빼면 서비스까지 멈춘다. 모니터링은 본문의 ``status`` 로
    알람을 건다. 인증하지 않으므로 내부망에만 노출한다.
    """
    monitor, problem = get_monitor()
    if monitor is None:
        body: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "status": UNKNOWN,
            "reason": problem,
            "databases": {},
        }
    else:
        body = monitor.report()
    strict = request.GET.get("strict", "").lower() in _STRICT_TRUE
    code = 503 if strict and body["status"] != CAUGHT_UP else 200
    response = JsonResponse(body, status=code)
    response["Cache-Control"] = "no-store"
    return response
