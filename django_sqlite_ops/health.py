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

import dataclasses
import json
import logging
import math
import os
import re
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
from .wal import IN_SYNC, NO_EVIDENCE, PENDING, WalEvidence, compare

__all__ = [
    "BACKLOG",
    "CAUGHT_UP",
    "DEFAULT_BACKLOG_GRACE",
    "DEFAULT_REFRESH",
    "LOGGER_NAME",
    "SCHEMA_VERSION",
    "STALE_FACTOR",
    "UNKNOWN",
    "AliasConfig",
    "FileTimes",
    "HealthConfig",
    "Monitor",
    "Sample",
    "Since",
    "alias_status",
    "file_times",
    "files_went_backwards",
    "get_monitor",
    "health_view",
    "next_backlog_since",
    "next_pending_since",
    "next_wal_pending_since",
    "overall_status",
    "parse_config",
    "read_boot_state",
    "redact",
    "wal_evidence",
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


# 원문 진단(Litestream stderr, 예외)은 응답에 넣지 않고 이 로거로만 남긴다.
LOGGER_NAME = "django_sqlite_ops.health"
_log = logging.getLogger(LOGGER_NAME)

# 가리는 규칙은 여기 한 곳이다(redact()).
# URL 의 userinfo: scheme://user:pass@host → scheme://***@host
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@\"'`]+@")
_SECRET_WORDS = (
    r"(?:secret|passw(?:or)?d|pwd|token|signature|sig|credential|access[-_]?key|api[-_]?key|auth)"
)
# URL 쿼리의 자격 증명류 값: ?X-Amz-Signature=... · &token=...
_QUERY_SECRET = re.compile(rf"(?i)([?&;][^=&\s]*{_SECRET_WORDS}[^=&\s]*=)[^&\s\"'`]*")
# key: value · key=value 꼴(YAML·환경 변수 메시지): secret-access-key: abc
_KV_SECRET = re.compile(
    rf"(?i)\b([\w.-]*{_SECRET_WORDS}[\w.-]*)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&)\]}}]+)"
)


def redact(text: str) -> str:
    """로그에 남기기 전에 URL userinfo·자격 증명류 쿼리 값·``key: value`` 비밀값을 가린다.

    완전한 비밀 탐지기가 아니다. 그래서 원문은 응답에 넣지 않고(고정 사유만) 로그에만 남긴다.
    """
    text = _URL_USERINFO.sub(r"\1***@", text)
    text = _QUERY_SECRET.sub(r"\1***", text)
    return _KV_SECRET.sub(r"\1\2***", text)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _absolute(path: str) -> str:
    """작업 디렉터리를 붙이기만 한다. ``abspath()`` 처럼 ``..`` 를 접으면 D-15 검사를 우회한다."""
    return path if os.path.isabs(path) else os.path.join(os.getcwd(), path)


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
    상대 경로는 이 프로세스의 작업 디렉터리를 앞에 붙인다. ``..``·링크는 접지 않는다(D-15 검사는
    갱신 때 원문으로 한다).
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
                _absolute(config),
                _absolute(meta_text) if meta_text is not None else None,
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
class Since:
    """어떤 상태를 처음 관측한 시점. ``mono`` 는 지속 시간 계산용, ``wall`` 은 표시용이다."""

    mono: float
    wall: float
    key: Any = None


@dataclass(frozen=True, slots=True)
class FileTimes:
    """DB 변경 시각과 최신 로컬 L0 의 시각(벽시계 mtime). ``ltx_key`` 는 (이름, mtime_ns).

    보조 근거다(WAL 위치로 판정할 수 없을 때만 쓴다). ``db_mtime``·``wal_mtime`` 은 파일 시각
    역행을 보려고 따로 둔다.
    """

    db_changed_at: float | None
    ltx_at: float | None
    ltx_key: Any = None
    db_mtime: float | None = None
    wal_mtime: float | None = None


@dataclass(frozen=True, slots=True)
class Sample:
    """한 번의 갱신에서 본 별칭 하나의 사실. 판정은 ``alias_status()`` 가 한다.

    ``observed`` 는 monotonic 초(지속 시간 계산), ``checked_at`` 은 UNIX 초(표시용).
    ``error`` 는 조회 전에 판정이 끝난 사유(경로 규칙 위반, 파일 DB 가 아님, 갱신 중 예외)이고,
    있으면 다른 필드와 무관하게 ``unknown`` 이다. 응답에 나가므로 고정 문장만 넣는다.
    """

    observed: float
    checked_at: float
    path: str | None = None
    local: int | None = None
    remote: Remote | None = None
    boot_state: dict[str, Any] | None = None
    boot_state_error: str | None = None
    error_code: str | None = None
    error: str | None = None
    files: FileTimes | None = None
    backlog_since: Since | None = None
    pending_since: Since | None = None
    wal: WalEvidence | None = None
    backwards: bool = False


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
    picked = {key: body.get(key) for key in _BOOT_KEYS}
    return {k: redact(v) if isinstance(v, str) else v for k, v in picked.items()}, None


# --- 판정 (순수 함수) ------------------------------------------------------------------


def next_backlog_since(
    prev: Since | None, local: int | None, remote: Remote | None, observed: float, wall: float
) -> Since | None:
    """아직 올라가지 않은 로컬 TXID 를 처음 관측한 시점. 로컬이 앞서 있지 않으면 ``None``.

    ``key`` 는 그때의 로컬 TXID 다. 원격이 그 TXID 에 닿으면(올라감) 지금의 로컬 TXID 로 다시
    센다. 그래서 지속 시간은 "관측한 미업로드 TXID 가 기다린 시간"이다: 쓰기가 계속되어 매 관측
    순간 로컬이 한 걸음 앞서도 업로드가 따라오면 쌓이지 않고(실측: 30ms 간격 쓰기에서 0.5초마다
    보면 늘 앞서 있다), 업로드가 쓰기를 못 따라가면 쌓인다. 프로세스 안에서만 추적하므로 재시작하면
    초기화된다.
    """
    if not (type(remote) is RemoteTxid and type(local) is int and local > remote.txid):
        return None
    if prev is not None and type(prev.key) is int and remote.txid < prev.key:
        return prev
    return Since(observed, wall, local)


def next_wal_pending_since(
    prev: Since | None, wal: WalEvidence | None, observed: float, wall: float
) -> Since | None:
    """WAL 에 최신 L0 가 담지 않은 커밋이 있음을 처음 관측한 시점. 아니면 ``None``.

    판정의 주 근거다(시계를 쓰지 않는다). 최신 L0 의 WAL 위치가 바뀌면(``wal.key``) 다시 센다:
    Litestream 이 진행하는 한 바쁜 DB 에서도 지속 시간이 쌓이지 않는다.
    """
    if wal is None or wal.state != PENDING:
        return None
    key = ("wal", wal.key)
    if prev is not None and prev.key == key:
        return prev
    return Since(observed, wall, key)


def files_went_backwards(prev: FileTimes | None, cur: FileTimes | None) -> bool:
    """DB·WAL·최신 L0 의 mtime 중 하나라도 앞선 관측보다 이르면 참(벽시계 역행 등).

    파일마다 앞뒤 관측 모두에 있을 때만 비교한다(``-wal`` 이 지워졌다 생기는 것은 역행이 아니다).
    """
    if prev is None or cur is None:
        return False
    pairs = (
        (prev.db_mtime, cur.db_mtime),
        (prev.wal_mtime, cur.wal_mtime),
        (prev.ltx_at, cur.ltx_at),
    )
    return any(a is not None and b is not None and b < a for a, b in pairs)


def next_pending_since(
    prev: Since | None, files: FileTimes | None, observed: float, wall: float
) -> Since | None:
    """(보조) DB 파일이 최신 로컬 L0 보다 새로워진 것을 처음 관측한 시점. 아니면 ``None``.

    WAL 위치로 판정할 수 없을 때만 쓴다. **파일 시각의 순서는 그 커밋이 L0 에 들어갔다는 증거가
    아니다**(L0 는 복사를 시작할 때 WAL 범위를 정하고 끝날 때 mtime 이 찍힌다 — review-2 재현).
    그래서 이 근거는 backlog 쪽으로만 쓰고, 아니라고 해서 ``in_sync`` 로 단정하지 않는다.
    Litestream 은 DB 변경을 L0 로 기록하므로(``monitor-interval`` 기본 1초) 살아 있으면 곧 더 새
    L0 가 생긴다. 최신 L0 가 바뀌면(이름·
    mtime) 새로 센다: 쓰기가 계속되는 DB 에서 매 관측 순간 DB 가 조금 더 새로워도 Litestream 이
    진행하는 한 지속 시간이 쌓이지 않는다. mtime 끼리의 차이는 지속 시간으로 쓰지 않는다(오래된
    유휴 DB 에 첫 쓰기가 오면 차이가 바로 커 보인다).
    """
    if (
        files is None
        or files.db_changed_at is None
        or files.ltx_at is None
        or files.db_changed_at <= files.ltx_at
    ):
        return None
    if prev is not None and prev.key == files.ltx_key:
        return prev
    return Since(observed, wall, files.ltx_key)


def _wal_fields(wal: WalEvidence | None) -> dict[str, Any] | None:
    if wal is None:
        return None
    return {
        "evidence": wal.state,
        "reason": wal.reason,
        "ltx_wal_end": wal.ltx_end,
        "wal_commit_end": wal.commit_end,
    }


def _fields(sample: Sample | None, now: float) -> dict[str, Any]:
    if sample is None:
        return {
            "path": None,
            "local_txid": None,
            "remote_txid": None,
            "checked_at": None,
            "age": None,
            "backlog_since": None,
            "pending_since": None,
            "db_changed_at": None,
            "ltx_at": None,
            "wal": None,
            "boot_state": None,
            "boot_state_error": None,
        }
    remote = sample.remote.txid if type(sample.remote) is RemoteTxid else None
    files = sample.files
    return {
        "path": sample.path,
        "local_txid": _hex(sample.local),
        "remote_txid": _hex(remote),
        "checked_at": _iso(sample.checked_at),
        "age": round(now - sample.observed, 3),
        "backlog_since": _iso(sample.backlog_since.wall) if sample.backlog_since else None,
        "pending_since": _iso(sample.pending_since.wall) if sample.pending_since else None,
        "db_changed_at": _iso(files.db_changed_at) if files else None,
        "ltx_at": _iso(files.ltx_at) if files else None,
        "wal": _wal_fields(sample.wal),
        "boot_state": sample.boot_state,
        "boot_state_error": sample.boot_state_error,
    }


def _verdict(
    sample: Sample | None, now: float, refresh: float, grace: float
) -> tuple[str, str, str]:
    """``(상태, 사유 코드, 사유)``. 사유는 고정 문장에 경로·TXID·초만 넣는다(비밀값 없음)."""
    if sample is None:
        return UNKNOWN, "not_checked", "not checked yet; the first refresh has not finished"
    age = now - sample.observed
    if age < 0 or age > refresh * STALE_FACTOR:
        return (
            UNKNOWN,
            "stale",
            (
                f"the last check is {age:.0f}s old (more than {STALE_FACTOR} x REFRESH); "
                "the refresh thread may be stuck"
            ),
        )
    if sample.error is not None:
        return UNKNOWN, sample.error_code or "error", sample.error
    if sample.boot_state is not None and sample.boot_state.get("unknown_at_boot") is True:
        return (
            UNKNOWN,
            "unknown_at_boot",
            (
                "unknown_at_boot: boot proceeded with --on-unknown keep-local; replication state "
                "is unknown until the next boot that is not keep-local"
            ),
        )
    remote = sample.remote
    if type(remote) is RemoteError:
        return (
            UNKNOWN,
            "remote_error",
            (f"remote lookup failed (litestream ltx); details are in the {LOGGER_NAME} log"),
        )
    if type(remote) is RemoteEmpty:
        return (
            UNKNOWN,
            "remote_empty",
            (
                "the replica is empty (or the replica path/prefix is wrong; litestream reports "
                "both the same way)"
            ),
        )
    if type(remote) is not RemoteTxid:
        return UNKNOWN, "no_remote", "no remote result"
    if sample.local is None:
        return (
            UNKNOWN,
            "no_local_meta",
            ("no readable local Litestream metadata (is litestream replicate running?)"),
        )
    if remote.txid > sample.local:
        return (
            UNKNOWN,
            "remote_ahead",
            (
                f"the replica is ahead of local ({remote.txid:016x} > {sample.local:016x}); "
                "another machine may be writing to the same replica"
            ),
        )
    if remote.txid < sample.local:
        since = sample.backlog_since.mono if sample.backlog_since else sample.observed
        behind = sample.observed - since
        if behind >= grace:
            return (
                BACKLOG,
                "local_ahead",
                (
                    f"local has been ahead of the replica for {behind:.0f}s "
                    f"(BACKLOG_GRACE {grace:g}s)"
                ),
            )
    wal = sample.wal
    pending = sample.observed - sample.pending_since.mono if sample.pending_since else None
    if wal is None or wal.state not in (PENDING, IN_SYNC):
        # WAL 로는 판정할 수 없다. 파일 시각은 보조 근거라 backlog 쪽으로만 쓴다.
        why = wal.reason if wal is not None else "no WAL evidence"
        files = sample.files
        if sample.backwards:
            return (
                UNKNOWN,
                "file_time_backwards",
                (
                    "a DB, -wal or L0 file time moved backwards since the last check (clock "
                    f"change?) and the -wal gives no evidence ({why})"
                ),
            )
        if pending is not None and pending >= grace:
            return (
                BACKLOG,
                "db_not_replicated",
                (
                    f"DB files changed at {_iso(files.db_changed_at if files else None)} after "
                    f"the latest local L0 ({_iso(files.ltx_at if files else None)})"
                    f" and stayed so for {pending:.0f}s (BACKLOG_GRACE {grace:g}s); is the "
                    "replicate process running? (file times only; the -wal gives no evidence: "
                    f"{why})"
                ),
            )
        return (
            UNKNOWN,
            "no_wal_evidence",
            f"cannot tell whether every commit is in the latest local L0: {why}",
        )
    if wal.state == PENDING and pending is not None and pending >= grace:
        return (
            BACKLOG,
            "db_not_replicated",
            (
                f"the -wal has commits after the latest local L0 (L0 ends at WAL byte "
                f"{wal.ltx_end}) for {pending:.0f}s (BACKLOG_GRACE {grace:g}s); is the "
                "replicate process running?"
            ),
        )
    if remote.txid < sample.local:
        behind = sample.observed - (
            sample.backlog_since.mono if sample.backlog_since else sample.observed
        )
        return (
            CAUGHT_UP,
            "local_ahead_within_grace",
            (f"local is ahead of the replica for {behind:.0f}s, within BACKLOG_GRACE {grace:g}s"),
        )
    if wal.state == PENDING:
        return (
            CAUGHT_UP,
            "db_changed_within_grace",
            (
                f"the -wal has commits after the latest local L0 for {pending or 0:.0f}s, "
                f"within BACKLOG_GRACE {grace:g}s"
            ),
        )
    return (
        CAUGHT_UP,
        "in_sync",
        "local and replica are at the same TXID and every -wal commit is in the latest L0",
    )


def alias_status(
    sample: Sample | None, now: float, *, refresh: float, grace: float
) -> dict[str, Any]:
    """별칭 하나의 상태(응답의 ``databases[alias]``). ``now`` 는 monotonic 초다.

    판정 순서(처음 맞는 것): 조회 전 · 오래된 결과(``REFRESH × 3`` 초과) · 사전 오류 ·
    ``unknown_at_boot`` · 원격 실패 · 원격 빈 목록 · 로컬 메타 없음 · 원격이 앞섬 → ``unknown``.
    로컬 TXID 가 앞선 지속 시간 ≥ ``grace`` → ``backlog``. 그다음 WAL 위치(주 근거)로 판단할 수
    없으면: 파일 시각 역행 → ``unknown``, 파일 시각(보조)이 grace 이상 L0 보다 새로움 →
    ``backlog``, 그 밖 → ``unknown``(``caught_up`` 으로 단정하지 않는다). WAL 에 L0 뒤 커밋이
    grace 이상 남음 → ``backlog``. 그 밖은 ``caught_up``. 지속 시간은 monotonic 관측
    시각(``observed``)끼리의 차이다.
    """
    status, code, reason = _verdict(sample, now, refresh, grace)
    return {"status": status, "code": code, "reason": _one_line(reason), **_fields(sample, now)}


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
        return None, f"not a writable file database (role {target.role})"
    if target.path is None:
        return None, "the database path is empty"
    return target.path, None


def _check_real(path: str, what: str) -> tuple[str | None, str | None]:
    """D-15: ``(실제 경로, 문제)``. 원문의 ``..``·부모 링크를 접지 않고 boot 의 규칙으로 본다."""
    try:
        real = real_path(path)
    except ValueError as exc:
        _log.debug("%s %s rejected: %s", what, path, redact(str(exc)))
        return None, (
            f"{what} {path} is not a real path (symlink or '..' in it, or the parent is "
            "missing); use the real path (D-15)"
        )
    if os.path.islink(real):
        return None, f"{what} {real} is a symbolic link; use the real path (D-15)"
    return str(real), None


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except (OSError, ValueError):
        return None


def file_times(db: str, meta: str | None) -> FileTimes:
    """DB 변경 시각 ``max(mtime(DB), mtime(DB-wal))``(있는 것만)과 최신 로컬 L0 파일의 시각.

    ``-shm`` 은 읽기에도 바뀌므로 보지 않는다. ``-wal`` 만 볼 수는 없다: 마지막 연결이 닫히면
    SQLite 가 체크포인트한 뒤 ``-wal`` 을 지운다(빌드에 따라 남긴다). L0 후보는
    ``local_max_txid()`` 와 같은 것이다.

    0.5.17 실측(DESIGN §7): Litestream 이 도는 동안에는 앱의 읽기·쓰기·Litestream 의 체크포인트
    뒤 곧(``monitor-interval`` 1초) 새 L0 가 생겨 DB 가 L0 보다 새로운 상태가 이어지지 않는다.
    멈춰 있으면 앱의 마지막 연결이 닫힐 때의 체크포인트(읽기만 했어도)가 DB mtime 을 바꾸고,
    ``replicate -once`` 가 끝날 때도 바뀐다(SIGTERM 종료는 바꾸지 않았다). 그래서 이 근거가
    grace 를 넘기면 "쓰기가 복제되지 않았다"가 아니라 "DB 가 쓰이는데 Litestream 이 진행하지
    않는다"로 읽는다.
    """
    db_mtime, wal_mtime = _mtime(db), _mtime(db + "-wal")
    times = [t for t in (db_mtime, wal_mtime) if t is not None]
    found = litestream.local_max_ltx(db, meta_path=meta)
    ltx_at = ltx_key = None
    if found is not None:
        try:
            st = os.stat(found[1])
            ltx_at, ltx_key = st.st_mtime, (found[1].name, st.st_mtime_ns)
        except OSError:
            pass
    return FileTimes(max(times) if times else None, ltx_at, ltx_key, db_mtime, wal_mtime)


def wal_evidence(db: str, meta: str | None) -> WalEvidence:
    """최신 로컬 L0 의 WAL 위치와 현재 ``-wal`` 의 커밋 위치를 비교한다(``wal.compare``).

    ``key`` 는 (L0 이름, L0 의 WAL 끝, salt) — L0 가 바뀌면 pending 을 다시 센다.
    """
    found = litestream.local_max_ltx(db, meta_path=meta)
    if found is None:
        return WalEvidence(NO_EVIDENCE, "no readable local L0")
    rng = litestream.ltx_wal_range(found[1])
    evidence = compare(db + "-wal", rng)
    key = None if rng is None else (found[1].name, rng.end, rng.salt1, rng.salt2)
    return dataclasses.replace(evidence, key=key)


Probe = Callable[[AliasConfig, str], Remote]
LocalProbe = Callable[[str, str | None], int | None]
FileProbe = Callable[[str, str | None], FileTimes | None]
WalProbe = Callable[[str, str | None], WalEvidence | None]


def _remote(cfg: AliasConfig, db: str) -> Remote:
    return litestream.remote_max_txid(db, config=cfg.litestream_config, binary=cfg.litestream)


def _local(db: str, meta: str | None) -> int | None:
    return litestream.local_max_txid(db, meta_path=meta)


@dataclass
class Monitor:
    """프로세스당 하나. 데몬 스레드가 ``refresh`` 초마다 ``samples`` 를 갱신한다.

    ``clock`` 은 표시용 벽시계, ``monotonic`` 은 지속 시간(grace·stale·pending)용이다.
    """

    config: HealthConfig
    remote: Probe = _remote
    local: LocalProbe = _local
    files: FileProbe = file_times
    wal: WalProbe = wal_evidence
    path_of: Callable[[str], tuple[str | None, str | None]] = _db_path
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    samples: dict[str, Sample] = field(default_factory=dict, init=False)
    refreshes: int = field(default=0, init=False)
    _logged: dict[str, str] = field(default_factory=dict, init=False)
    _pid: int | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # -- 갱신 --

    def _now(self) -> tuple[float, float]:
        return self.monotonic(), self.clock()

    def _log_once(self, alias: str, message: str) -> None:
        """원문 진단은 로그로만(가린 뒤). 같은 메시지를 갱신마다 반복하지 않는다."""
        message = redact(message)
        if self._logged.get(alias) != message:
            self._logged[alias] = message
            _log.warning("health %r: %s", alias, message)

    def _sample(self, cfg: AliasConfig, prev: Sample | None) -> Sample:
        observed, wall = self._now()
        path, problem = self.path_of(cfg.alias)
        if problem is not None or path is None:
            return Sample(
                observed, wall, error_code="not_file_db", error=problem or "no database path"
            )
        real, problem = _check_real(path, "database path")
        meta = None
        if problem is None and cfg.meta_path is not None:
            meta, problem = _check_real(cfg.meta_path, "meta_path")
        boot_state, boot_error = read_boot_state(real or path)
        if problem is not None or real is None:
            return Sample(
                observed,
                wall,
                path=path,
                boot_state=boot_state,
                boot_state_error=boot_error,
                error_code="path_not_real",
                error=problem,
            )
        remote = self.remote(cfg, real)
        if type(remote) is RemoteError:
            self._log_once(cfg.alias, f"remote lookup failed: {remote.message}")
        else:
            self._logged.pop(cfg.alias, None)
        local = self.local(real, meta)
        wal = self.wal(real, meta)
        files = self.files(real, meta)
        observed, wall = self._now()
        backlog = next_backlog_since(
            prev.backlog_since if prev else None, local, remote, observed, wall
        )
        prev_pending = prev.pending_since if prev else None
        backwards = files_went_backwards(prev.files if prev else None, files)
        if wal is not None and wal.state in (PENDING, IN_SYNC):
            pending = next_wal_pending_since(prev_pending, wal, observed, wall)
        elif backwards:
            pending = prev_pending  # 역행했다고 앞선 미복제 근거를 지우지 않는다
        else:
            pending = next_pending_since(prev_pending, files, observed, wall)
        return Sample(
            observed,
            wall,
            real,
            local,
            remote,
            boot_state,
            boot_error,
            files=files,
            backlog_since=backlog,
            pending_since=pending,
            wal=wal,
            backwards=backwards,
        )

    def refresh_once(self) -> None:
        """모든 별칭을 한 번 갱신한다. 예외는 삼키고 그 별칭을 ``unknown`` 사유로 남긴다."""
        for cfg in self.config.databases:
            with self._lock:
                prev = self.samples.get(cfg.alias)
            try:
                sample = self._sample(cfg, prev)
            except Exception as exc:  # noqa: BLE001 - 스레드가 죽지 않게 사유로 남긴다
                try:
                    self._log_once(cfg.alias, f"refresh failed: {type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    observed, wall = self._now()
                except Exception:  # noqa: BLE001
                    observed, wall = time.monotonic(), time.time()
                sample = Sample(
                    observed,
                    wall,
                    error_code="refresh_failed",
                    error=f"refresh failed ({type(exc).__name__}); details are in the "
                    f"{LOGGER_NAME} log",
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
            self._logged = {}
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
        """``now`` 는 monotonic 초(없으면 지금)."""
        now = self.monotonic() if now is None else now
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


def get_monitor() -> tuple[Monitor | None, str | None, str | None]:
    """설정으로 만든 프로세스의 ``Monitor`` 를 돌려주고 스레드를 시작한다.

    ``(모니터, 사유 코드, 사유)``. 설정 오류의 원문(설정값이 들어 있다)은 응답에 넣지 않는다.
    """
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            config, errors = load_config()
            if config is None:
                if errors:
                    return (
                        None,
                        "invalid_config",
                        ("invalid SQLITE_OPS['HEALTH']; run manage.py check (sqlite_ops.E002)"),
                    )
                return None, "not_configured", "SQLITE_OPS['HEALTH'] is not configured"
            _monitor = Monitor(config)
        _monitor.ensure_started()
        return _monitor, None, None


# --- 뷰 --------------------------------------------------------------------------------

_STRICT_TRUE = frozenset({"1", "true", "yes", "on"})


class _AllAliases(set):
    """모든 별칭을 포함하는 ``_non_atomic_requests``.

    Django 5.2·6.1 의 ``BaseHandler.make_view_atomic()`` 은 ``ATOMIC_REQUESTS`` 가 켜진 별칭마다
    ``alias not in view._non_atomic_requests`` 일 때만 뷰를 ``atomic(using=alias)`` 로 감싼다
    (5.2.18·6.1.2 ``django/core/handlers/base.py``). ``transaction.non_atomic_requests(using)`` 은
    별칭 하나만 넣으므로, 설정에 어떤 별칭이 있든 모두 빠지게 이 집합을 쓴다. 감싸면 뷰 실행 전에
    연결이 열리고 DB 파일이 생긴다(review-1 재현).
    """

    def __contains__(self, alias: object) -> bool:
        return True


def health_view(request: HttpRequest) -> JsonResponse:
    """복제 헬스 JSON. HTTP 상태는 항상 200 이다(``?strict=1`` 이면 ``caught_up`` 이 아닐 때 503).

    복제가 뒤처졌다고 로드밸런서가 앱을 빼면 서비스까지 멈춘다. 모니터링은 본문의 ``status`` 로
    알람을 건다. 인증하지 않으므로 내부망에만 노출한다. 요청 트랜잭션(``ATOMIC_REQUESTS``)에서
    모든 별칭이 빠진다.
    """
    monitor, code, problem = get_monitor()
    if monitor is None:
        body: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "status": UNKNOWN,
            "code": code,
            "reason": problem,
            "databases": {},
        }
    else:
        body = monitor.report()
    strict = request.GET.get("strict", "").lower() in _STRICT_TRUE
    status = 503 if strict and body["status"] != CAUGHT_UP else 200
    response = JsonResponse(body, status=status)
    response["Cache-Control"] = "no-store"
    return response


health_view._non_atomic_requests = _AllAliases()  # type: ignore[attr-defined]
