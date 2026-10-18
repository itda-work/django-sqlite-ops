"""``sqlite_doctor --restore-test``: 복제본을 실제로 복원해 본다 (DESIGN §6-2, #52·#54).

헬스(§7)는 "지금 복제가 따라오는가"만 본다. 이 모드는 "복제본으로 실제 복구할 수 있는가"를 본다:
``--restore-dir`` 아래 새 작업 디렉터리에 복원하고, 복원본의 무결성과 TXID 를 확인한다.

**Django DB 연결을 열지 않는다.** 대상 경로는 ``get_connection_params()`` 로만 정하고(연결하지
않는다), 복원본은 표준 ``sqlite3`` 의 ``mode=ro&immutable=1`` URI 로 연다. 그래서 일반 doctor 와
달리 ``init_command`` 부작용이 없다.

운영 자원(운영 DB·사이드카·메타·boot 소유 경로·DB 부모 디렉터리)과 겹치는 ``--restore-dir`` 은
아무것도 만들기 전에 거부한다. ``litestream restore -o OUT`` 은 DB_PATH 쪽을 읽지도 쓰지도 않는다
(0.5.17, 전후 해시·mtime 과 strace 로 재현함, #54). 출력 옆에 ``OUT.tmp`` 를 만든 뒤 rename 한다.

Litestream 호출은 boot 헬퍼(``boot/litestream.py``)를, 경로 계약은 boot CLI 의 ``real_path()``
(D-15)와 §4-2 비교 함수를 그대로 쓴다.
"""

import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.db import connections

from .boot import litestream
from .boot.cli import _fold, _overlaps, lock_path, real_path, sidecars, state_path
from .boot.decide import RemoteEmpty, RemoteError
from .doctor import (
    Item,
    _binary,
    _caller_path,
    _file_size,
    _one_line,
    _Target,
    _target,
    select_aliases,
)
from .health import load_config, redact

__all__ = [
    "DEFAULT_INTEGRITY_TIMEOUT",
    "INTEGRITY_MODES",
    "SECTION",
    "check_integrity",
    "check_restore_dir",
    "protected_paths",
    "restore_test",
]

SECTION = "restore"
INTEGRITY_MODES = ("full", "quick")
# 무결성 검사의 별도 예산(복원 시간과 합치지 않는다). 700MB DB 의 full 검사가 2.6초였다(#54 실측).
DEFAULT_INTEGRITY_TIMEOUT = 1800.0
# 복원 직전 원격 조회(ltx -level all)의 시간 제한. 테스트가 줄일 수 있도록 모듈 값으로 둔다.
_LTX_TIMEOUT = litestream.DEFAULT_LTX_TIMEOUT
# 필요한 여유 공간 = (로컬 DB + -wal) × 1.2 (분수로 둔다). 로컬 크기로 복원본 크기를 추정한다.
_SPACE_FACTOR = (6, 5)
# 무결성 실패 때 message 에 담는 오류 줄 수.
_MAX_PROBLEMS = 5
_PRAGMAS = {"full": "integrity_check", "quick": "quick_check"}
NO_TARGET = "no write file database to restore-test"


def _item(alias: str | None, key: str, level: str, value=None, expected=None, message=""):
    return Item(SECTION, alias, key, level, value, expected, message)


def _hex(txid: int | None) -> str | None:
    return None if txid is None else f"{txid:016x}"


# --- 운영 경로 보호 (D-25) ----------------------------------------------------------------


def protected_paths(db: Path, meta: Path) -> list[tuple[str, Path]]:
    """``--restore-dir`` 이 겹치면 안 되는 운영 경로 ``(역할, 경로)``.

    DB·사이드카·boot 잠금·상태 파일·메타·DB 부모 디렉터리. 링크일 수 있는 것(메타는 D-15 조회
    규약상 링크를 허용한다)은 실체(``realpath``)도 넣는다. boot 의 다른 예약 이름(``.restore-*``·
    ``.stale-*``·상태 임시 파일)은 모두 DB 부모 디렉터리 안이라 부모 규칙이 함께 막는다.
    """
    named = [
        ("database", db),
        *(("sidecar", s) for s in sidecars(db)),
        ("boot lock", lock_path(db)),
        ("boot state file", state_path(db)),
        ("litestream meta", meta),
        ("database directory", db.parent),
    ]
    out: list[tuple[str, Path]] = []
    for what, path in named:
        out.append((what, path))
        real = Path(os.path.realpath(path))
        if real != path:
            out.append((f"{what} (resolved)", real))
    return out


def _fold_overlaps(a: Path, b: Path) -> bool:
    """대소문자·유니코드 정규형을 무시한 경로 문자열 비교(``_fold``). 아직 없는 경로도 막는다."""
    fa, fb = _fold(str(a)), _fold(str(b))
    return fa == fb or fb.startswith(fa.rstrip(os.sep) + os.sep) or fa.startswith(fb + os.sep)


def check_restore_dir(
    raw: str | os.PathLike[str], protected: Iterable[tuple[str | None, str, Path]]
) -> tuple[Path | None, str | None]:
    """``--restore-dir`` 을 검사한다. ``(실제 경로, None)`` 또는 ``(None, 사유)``.

    마지막 구성요소까지 링크가 아닌 실제 디렉터리여야 한다: 부모는 ``real_path()``(D-15), 마지막
    구성요소는 ``lstat`` 으로 본다. ``protected`` 는 ``(별칭, 역할, 경로)`` 이고, 그중 하나와
    같거나 조상·자손이면 거부한다(경로 문자열·``(st_dev, st_ino)``·``_fold`` 비교). 아무것도
    만들지 않는다.
    """
    text = os.fspath(raw)
    if not text:
        return None, "--restore-dir is empty"
    stripped = text.rstrip(os.sep)
    if not stripped:
        return None, f"--restore-dir {text} is the filesystem root"
    if os.path.basename(stripped) in (".", ".."):
        return None, f"--restore-dir {text}: pass the directory's real absolute path"
    try:
        path = real_path(stripped)
    except ValueError as exc:
        return None, f"--restore-dir: {exc}"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, f"--restore-dir {path} does not exist; create it first"
    except OSError as exc:
        return None, f"--restore-dir {path}: cannot stat: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return None, f"--restore-dir {path} is a symlink; pass the real directory"
    if not stat.S_ISDIR(st.st_mode):
        return None, f"--restore-dir {path} is not a directory"
    for alias, what, other in protected:
        try:
            hit = _overlaps(path, other) or _fold_overlaps(path, other)
        except OSError as exc:
            return None, f"--restore-dir {path}: cannot compare with {other}: {exc}"
        if hit:
            return None, (
                f"--restore-dir {path} overlaps the {what} of alias {alias!r}: {other}; "
                "use a directory outside the database directory and the litestream meta"
            )
    return path, None


# --- 무결성 -------------------------------------------------------------------------------


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(os.path.abspath(path))}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def check_integrity(
    path: Path, mode: str, timeout: float, *, alias: str | None = None
) -> tuple[bool, list[Item]]:
    """복원본에 ``PRAGMA integrity_check``/``quick_check`` 를 우리가 실행한다(D-24).

    ``(무결성 ok 여부, 항목)``. 항목은 ``integrity`` 하나와, 검사가 끝까지 돌았으면 구조 요약
    (``tables``·``page_count``)이다. 시간이 ``timeout`` 을 넘으면 타이머 스레드가
    ``Connection.interrupt()`` 로 끊는다(700MB full 검사에서 1ms 안에 멈춤, #54 실측).
    """
    pragma = _PRAGMAS[mode]
    try:
        conn = _connect_ro(path)
    except sqlite3.Error as exc:
        return False, [_item(alias, "integrity", "error", mode, None, f"cannot open: {exc}")]
    try:
        fired = threading.Event()

        def stop() -> None:
            fired.set()
            conn.interrupt()

        timer = threading.Timer(timeout, stop)
        timer.daemon = True
        start = time.monotonic()
        timer.start()
        try:
            rows = conn.execute(f"PRAGMA {pragma}").fetchall()
            failure = None
        except sqlite3.Error as exc:
            rows, failure = None, exc
        finally:
            timer.cancel()
            timer.join()
        elapsed = time.monotonic() - start
        took = f"{elapsed * 1000:.0f} ms"
        if failure is not None:
            if fired.is_set() and "interrupt" in str(failure):
                message = (
                    f"timeout: {pragma} did not finish within --integrity-timeout {timeout:g}s "
                    f"(interrupted after {elapsed:.1f}s)"
                )
            elif fired.is_set():
                # 타이머가 발동했지만 다른 오류로 끝났다: 두 사실을 함께 적는다(리뷰 1 F8).
                message = (
                    f"{pragma} failed after {took}: {failure} "
                    f"(the --integrity-timeout {timeout:g}s timer had also fired)"
                )
            else:
                message = f"{pragma} failed after {took}: {failure}"
            return False, [_item(alias, "integrity", "error", mode, None, message)]
        if rows != [("ok",)]:
            shown = " | ".join(str(r[0]) for r in rows[:_MAX_PROBLEMS])
            more = f" (+{len(rows) - _MAX_PROBLEMS} more)" if len(rows) > _MAX_PROBLEMS else ""
            return False, [
                _item(alias, "integrity", "error", mode, None, f"{pragma}: {shown}{more}"),
                *_structure(conn, alias),
            ]
        return True, [
            _item(alias, "integrity", "ok", mode, None, f"{pragma}: ok ({took})"),
            *_structure(conn, alias),
        ]
    finally:
        conn.close()


def _structure(conn: sqlite3.Connection, alias: str | None) -> list[Item]:
    """복원본 구조 요약(보고 전용). 읽지 못하면 ``tables`` 하나를 ``error`` 로."""
    try:
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()
        pages = conn.execute("PRAGMA page_count").fetchone()
        size = conn.execute("PRAGMA page_size").fetchone()
    except sqlite3.Error as exc:
        return [_item(alias, "tables", "error", None, None, f"cannot read the schema: {exc}")]
    return [
        _item(alias, "tables", "ok", tables[0]),
        _item(alias, "page_count", "ok", pages[0], None, f"page_size {size[0]}"),
    ]


# --- 정리 ---------------------------------------------------------------------------------


def _finish(alias: str, work: Path, passed: bool, keep: bool) -> list[Item]:
    """``restore``·``integrity`` 가 둘 다 ok 면 작업 디렉터리를 지우고, 아니면 남긴다.

    TXID 가 원격에 못 미치거나(``warn``) 원격을 조회하지 못했어도(``unknown``) 지운다: 산출물은
    그 원인(원격 쪽 상태)을 설명하지 않는다.
    """
    if keep:
        return [_item(alias, "output", "ok", str(work), None, "kept (--keep)")]
    if not passed:
        message = "kept for inspection; remove it when done"
        return [_item(alias, "output", "ok", str(work), None, message)]
    try:
        shutil.rmtree(work)
    except OSError as exc:
        return [_item(alias, "output", "warn", str(work), None, f"cleanup failed: {exc}")]
    return []


# --- 별칭 하나 ------------------------------------------------------------------------------


def _one(
    alias: str,
    db: Path,
    root: Path,
    *,
    config: str,
    binary: str,
    integrity: str,
    restore_timeout: float,
    integrity_timeout: float,
    keep: bool,
    identity: tuple[int, int] | None = None,
) -> list[Item]:
    items: list[Item] = []
    # 1. 용량: 이 별칭을 시작하기 직전의 여유로 본다(앞 별칭이 남긴 산출물이 자연히 반영된다).
    local = (_file_size(str(db)) or 0) + (_file_size(f"{db}-wal") or 0)
    need = -(-local * _SPACE_FACTOR[0] // _SPACE_FACTOR[1])  # 정수 올림(부동소수 오차 없이)
    estimate = f"estimated from the local db + -wal size ({local} bytes) x 1.2"
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        return [_item(alias, "space", "error", None, need, f"cannot read free space: {exc}")]
    if free < need:
        return [_item(alias, "space", "error", free, need, f"not enough free space; {estimate}")]
    items.append(_item(alias, "space", "ok", free, need, estimate))

    # 2. 작업 디렉터리: 실행마다 새로 만든다(0700). 겹치는 실행끼리 충돌하지 않는다. 검사한
    #    --restore-dir 이 그대로인지(같은 디렉터리, 링크 아님) 바로 앞에서 다시 본다(R5-4).
    if identity is None or _dir_identity(root) != identity:
        message = "--restore-dir changed since it was checked (replaced or turned into a symlink)"
        items.append(_item(alias, "restore_dir", "error", str(root), None, message))
        return items
    try:
        work = Path(tempfile.mkdtemp(prefix="dso-restore-", dir=root))
    except OSError as exc:
        items.append(_item(alias, "restore_dir", "error", str(root), None, f"mkdtemp: {exc}"))
        return items
    items.append(_item(alias, "restore_dir", "ok", str(work)))
    out = work / db.name

    # 3. 복원 직전 원격 최대 TXID.
    remote = litestream.remote_max_txid(db, config=config, binary=binary, timeout=_LTX_TIMEOUT)
    if isinstance(remote, RemoteEmpty):
        items.append(
            _item(
                alias,
                "remote_txid",
                "error",
                None,
                None,
                "the replica lists no LTX files: nothing to restore (litestream 0.5.17 reports a "
                "mistyped replica path the same way)",
            )
        )
        return items + _finish(alias, work, False, keep)
    remote_txid = None
    if isinstance(remote, RemoteError):
        items.append(_item(alias, "remote_txid", "unknown", None, None, redact(remote.message)))
    else:
        remote_txid = remote.txid
        items.append(_item(alias, "remote_txid", "ok", _hex(remote_txid)))

    # 4. 복원. 무결성은 Litestream 에 맡기지 않는다(D-24): 실패 사유가 한 줄이라 내려받기
    #    실패와 구분되지 않는다.
    start = time.monotonic()
    result = litestream.restore(
        db, out, config=config, binary=binary, timeout=restore_timeout, integrity_check="none"
    )
    duration = round((time.monotonic() - start) * 1000)
    if not result.ok:
        items.append(_item(alias, "restore", "error", "failed", None, redact(result.reason)))
        items.append(_item(alias, "duration_ms", "ok", duration))
        return items + _finish(alias, work, False, keep)
    items.append(_item(alias, "restore", "ok", "restored", None, str(out)))
    if remote_txid is None:
        items.append(
            _item(
                alias,
                "restored_txid",
                "unknown",
                _hex(result.txid),
                None,
                "the remote TXID was not available; not compared",
            )
        )
    elif result.txid >= remote_txid:
        items.append(_item(alias, "restored_txid", "ok", _hex(result.txid), _hex(remote_txid)))
    else:
        items.append(
            _item(
                alias,
                "restored_txid",
                "warn",
                _hex(result.txid),
                _hex(remote_txid),
                "the restored TXID is below the remote TXID read just before the restore",
            )
        )
    items.append(_item(alias, "duration_ms", "ok", duration))
    items.append(_item(alias, "size", "ok", _file_size(str(out))))

    # 5. 무결성(우리가 실행, 별도 시간 예산).
    passed, checked = check_integrity(out, integrity, integrity_timeout, alias=alias)
    items.extend(checked)
    return items + _finish(alias, work, passed, keep)


# --- 조립 ---------------------------------------------------------------------------------


def _meta_paths() -> tuple[dict[str, str], str | None]:
    """헬스 설정의 별칭별 ``meta_path``. 설정이 잘못됐으면 ``({}, 사유)``."""
    health, errors = load_config()
    if errors:
        return {}, "; ".join(message for message, _ in errors)
    if health is None:
        return {}, None
    return {c.alias: c.meta_path for c in health.databases if c.meta_path is not None}, None


def _base_paths(name: str) -> list[Path]:
    """보호 합집합의 기준 경로: 글자 그대로 접은 경로와, 커널처럼 링크를 따라간 실체(R5-1).

    ``abspath`` 는 ``..`` 를 글자 단위로 먼저 접으므로 ``srv/x/../data``(``srv/x`` 가 링크)의 실체를
    놓친다. ``realpath`` 는 구성요소를 차례로 풀어 링크 뒤의 ``..`` 를 커널과 같이 처리한다.
    DB 파일이 링크면 SQLite 는 실체 옆에 사이드카를 만들므로 실체 기준의 부모 디렉터리·사이드카도
    넣는다.
    """
    literal = Path(os.path.normpath(os.path.abspath(name)))
    real = Path(os.path.realpath(name))
    return [literal] if real == literal else [literal, real]


def _dir_identity(path: Path) -> tuple[int, int] | None:
    """링크가 아닌 디렉터리의 ``(st_dev, st_ino)``. 아니거나 읽지 못하면 ``None``."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return st.st_dev, st.st_ino


def _dir_entry(listed: Iterable[str], cwd: str) -> tuple[str, bool] | None:
    """``databases -json`` 목록에서 조회한 임시 cwd 기준으로 풀린 항목 ``(경로, dir: 항목인가)``.

    0.5.17 은 ``dir:`` 항목을 감시 디렉터리가 아니라 실행한 cwd 자체로 내보내고(절대·상대 ``dir:``
    모두), 상대 ``path:`` 는 cwd 아래로 푼다(재현함 #54). 앞은 감시 디렉터리를, 뒤는 replicate 가
    어디서 도느냐에 따라 바뀌는 DB 위치를 알 수 없다. 둘 다 거부한다(D-26).
    """
    roots = {os.path.normpath(cwd), os.path.realpath(cwd)}
    for path in listed:
        for root in roots:
            if path == root:
                return path, True
            if path.startswith(root.rstrip(os.sep) + os.sep):
                return path, False
    return None


def _sqlite_targets() -> tuple[dict[str, _Target], list[tuple[str, str]]]:
    """보호 합집합을 만들 별칭: ``vendor == "sqlite"`` 인 ``DATABASES`` 별칭 전부(D-25, R4-1).

    ENGINE 문자열 일치가 아니라 백엔드의 ``vendor`` 로 본다(sqlite3 를 상속한 사용자 백엔드·
    SpatiaLite 도 SQLite 파일을 쓴다). ``connections[alias]`` 는 래퍼만 만들고 연결하지 않는다.
    백엔드를 불러오지 못하는 별칭은 ``(별칭, 사유 한 줄)`` 로 따로 돌려준다(SQLite 인지 모른다).
    """
    targets: dict[str, _Target] = {}
    unloadable: list[tuple[str, str]] = []
    for alias, config in settings.DATABASES.items():
        try:
            vendor = connections[alias].vendor
        except Exception as exc:  # noqa: BLE001 - ImproperlyConfigured·ImportError 등을 한 줄로
            unloadable.append((alias, _one_line(f"{type(exc).__name__}: {exc}")))
            continue
        if vendor == "sqlite":
            targets[alias] = _target(alias, config)
    return targets, unloadable


def restore_test(
    aliases: Iterable[str] | None,
    *,
    restore_dir: str,
    litestream_config: str,
    litestream_binary: str = "litestream",
    integrity: str = "full",
    restore_timeout: float = litestream.DEFAULT_RESTORE_TIMEOUT,
    integrity_timeout: float = DEFAULT_INTEGRITY_TIMEOUT,
    keep: bool = False,
) -> list[Item]:
    """별칭마다 차례로 복원 검증한 항목. 대상은 쓰기 파일 DB 뿐이다(DESIGN §6-2).

    ``aliases`` 가 없으면 쓰기 파일 DB 인 sqlite3 별칭만 실행하고 나머지는 ``alias`` ``ok``
    'skipped' 로 남긴다. 명시한 별칭이 쓰기 파일 DB 가 아니면 ``unknown`` 이다. 하나도 실행하지
    못했고 다른 ``error`` 도 없으면 ``alias`` ``error``(별칭 ``null``)다.
    """
    if integrity not in INTEGRITY_MODES:
        raise ValueError(f"integrity must be one of {INTEGRITY_MODES}, got {integrity!r}")
    explicit = aliases is not None
    _, selected, items = select_aliases(aliases, SECTION)
    items = list(items)
    metas, meta_error = _meta_paths()

    targets, unloadable = _sqlite_targets()
    protected: list[tuple[str | None, str, Path]] = []
    # 경로 계산이 ValueError(NUL 등)를 내는 별칭은 보호 경로를 정할 수 없다(R6-2).
    unresolvable: list[str] = []
    for alias, target in targets.items():
        if target.path is None:
            continue
        meta = metas.get(alias)
        try:
            for base in _base_paths(target.path):
                meta_path = (
                    Path(os.path.normpath(meta)) if meta else litestream.default_meta_path(base)
                )
                protected.extend((alias, what, p) for what, p in protected_paths(base, meta_path))
        except ValueError as exc:
            unresolvable.append(f"{alias!r} ({_one_line(str(exc))})")

    runnable: list[tuple[str, Path]] = []
    for alias in selected:
        target = targets[alias]
        if target.role == "error":
            items.append(_item(alias, "alias", "error", None, None, target.reason or ""))
            continue
        if target.role != "write" or target.path is None:
            # --database 로 고른 별칭이면 판정할 수 없다는 뜻이고, 기본 선택이면 대상이 아닐 뿐이다.
            level, message = ("unknown", "only write file databases are restore-tested")
            if not explicit:
                level, message = "ok", f"skipped ({target.role}): {message}"
            items.append(_item(alias, "alias", level, target.role, "write", message))
            continue
        try:
            db = real_path(target.path)
        except ValueError as exc:
            items.append(_item(alias, "database", "error", target.path, None, str(exc)))
            continue
        if db.is_symlink():
            # boot(D-15)와 같은 계약: DB 는 정규 파일 경로로만 받는다. SQLite 는 링크를 따라가
            # 실체 옆에 사이드카를 만들어, 이 경로와 Litestream 설정의 경로가 어긋난다.
            message = "the database file is a symlink; use the real file path (D-15)"
            items.append(_item(alias, "database", "error", str(db), None, message))
            continue
        runnable.append((alias, db))

    # 운영 경로와 겹치는지는 아무것도 만들기 전에, 모든 별칭의 합집합으로 본다.
    if meta_error is not None:
        items.append(
            _item(
                None,
                "restore_dir",
                "error",
                restore_dir,
                None,
                f"SQLITE_OPS['HEALTH'] is invalid, so the meta paths to protect are unknown: "
                f"{meta_error}",
            )
        )
        return items
    # 보호할 로컬 경로를 정할 수 없는 sqlite3 별칭(설정 오류 error, URI 판정 불가 unknown)이 있으면
    # 합집합이 불완전하다: 판정할 수 없으므로 거부한다(R2-1·R3-1). URI 에서 경로를 짐작해 꺼내지
    # 않는다. memory·vfs 는 로컬 DB 파일이 없으므로 통과한다.
    unknown = [
        f"{alias!r} (role {target.role}: {target.reason or 'no reason given'})"
        for alias, target in targets.items()
        if target.role in ("error", "unknown")
    ] + unresolvable
    if unloadable:
        names = ", ".join(f"{alias!r} ({reason})" for alias, reason in unloadable)
        items.append(
            _item(
                None,
                "restore_dir",
                "error",
                restore_dir,
                None,
                f"cannot load the database backend of alias {names}, so it is unknown whether "
                "it is a SQLite database whose paths must be protected; fix or remove the "
                "DATABASES entry",
            )
        )
        return items
    if unknown:
        items.append(
            _item(
                None,
                "restore_dir",
                "error",
                restore_dir,
                None,
                f"cannot determine the paths of alias {', '.join(unknown)}, so the operational "
                "paths to protect are unknown; fix the DATABASES entry",
            )
        )
        return items
    root, problem = check_restore_dir(restore_dir, protected)
    if problem is not None:
        items.append(_item(None, "restore_dir", "error", restore_dir, None, problem))
        return items
    # 별칭마다 mkdtemp 직전에 같은 디렉터리인지 다시 본다(앞 별칭의 복원·검사 동안 바뀔 수 있다).
    identity = _dir_identity(root)
    if not runnable:
        # 아무것도 검증하지 않고 성공하면 감시(cron·systemd)에서 거짓 초록이 된다(리뷰 1 F1).
        if not any(i.level == "error" for i in items):
            items.append(_item(None, "alias", "error", None, None, NO_TARGET))
        return items

    config = _caller_path(litestream_config)
    binary = _binary(litestream_binary)
    # 상대 경로·dir: 항목은 Litestream 작업 디렉터리 기준으로 풀리므로 새 임시 cwd 에서 읽는다.
    with tempfile.TemporaryDirectory(prefix="sqlite-doctor-") as cwd:
        listed = litestream.config_databases(config, binary=binary, cwd=cwd)
    if isinstance(listed, str):
        items.append(_item(None, "config", "error", config, None, redact(listed)))
        return items
    entry = _dir_entry(listed, cwd)
    if entry is not None:
        path, is_dir = entry
        if is_dir:
            message = (
                "the litestream config has a dir: entry: litestream databases lists it as its "
                f"working directory {path}, so the watched directory cannot be determined and a "
                "restore under --restore-dir may be picked up and replicated (D-26)"
            )
        else:
            message = (
                f"relative path {path} in the litestream config; its location depends on where "
                "litestream runs, so the paths to protect are unknown (D-26)"
            )
        items.append(_item(None, "config", "error", config, None, message))
        return items
    in_config = set(listed)
    for alias, db in runnable:
        if str(db) not in in_config:
            items.append(
                _item(
                    alias,
                    "database",
                    "error",
                    str(db),
                    None,
                    "not in the litestream config (dbs[].path must be this same real path, D-15)",
                )
            )
            continue
        items.extend(
            _one(
                alias,
                db,
                root,
                config=config,
                binary=binary,
                integrity=integrity,
                restore_timeout=restore_timeout,
                integrity_timeout=integrity_timeout,
                keep=keep,
                identity=identity,
            )
        )
    return items
