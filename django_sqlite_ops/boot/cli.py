"""boot CLI: 잠금 → 중단된 격리 재개 → 판정 → 조치 → 무결성 → 상태 파일 → exec (DESIGN §4-2).

``python -m django_sqlite_ops.boot --db PATH --config PATH [...] -- CMD [ARGS...]``

표준 라이브러리만 쓴다. Django 를 import 하지 않는다(DESIGN §4-1).
판정은 ``decide()`` 하나가 한다. 이 모듈은 입력을 모으고 조치를 실행한다. 예외는 판정에 넣지
않은 이상 상태 하나뿐이다: DB 파일 없이 ``-wal``/``-shm``/``-journal`` 이 남은 경우(§4-3).

순서가 안전성이다.
- 복원은 항상 DB 옆의 새 임시 디렉터리에 먼저 끝내고 검증한다. 성공했을 때만 로컬을 격리한다.
  그래서 복원이 실패하면 로컬은 하나도 바뀌지 않는다(L7).
- 격리는 ``<db>.stale-<ts>.partial/`` 로 대상을 하나씩 옮긴 뒤 디렉터리를 ``<db>.stale-<ts>/`` 로
  rename 한다. ``.partial`` 이 남아 있으면 격리 도중 죽은 것이고, 다음 부팅이 판정 전에 끝낸다(L6).
"""

import argparse
import errno
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import litestream as ls
from .decide import Action, OnUnknown, ReasonCode, RemoteError, RemoteTxid, State, decide

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "EXIT_EXEC",
    "EXIT_INTEGRITY",
    "EXIT_LOCK",
    "EXIT_REFUSE",
    "EXIT_RESTORE",
    "EXIT_USAGE",
    "main",
]

# DESIGN §4-5. 0 은 exec 라 돌아오지 않는다.
EXIT_REFUSE = 2
EXIT_INTEGRITY = 3
EXIT_RESTORE = 4
EXIT_LOCK = 5
EXIT_USAGE = 64
EXIT_EXEC = 127

# DB 옆에 남는 SQLite 파일. 격리 대상이며, DB 없이 이것만 있으면 이상 상태다.
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# DB 없이 사이드카만 남은 경우의 사유 코드. decide() 를 부르지 않고 CLI 가 정한다.
ORPHAN_SIDECARS = "orphan_sidecars"

_MANIFEST = "manifest.json"
# 원자적 쓰기의 임시 파일 이름: <이름>.tmp-<rand>
_TMP_INFIX = ".tmp-"
_STATE_VERSION = 1


class _Exit(Exception):
    """단계 함수가 부팅을 끝낼 때 던진다. ``main()`` 이 종료 코드로 바꾼다."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def log(message: str) -> None:
    print(f"[boot] {message}", file=sys.stderr, flush=True)


# --- 경로 --------------------------------------------------------------------------------


def real_path(raw: str | os.PathLike[str]) -> Path:
    """``--db``·``--meta-path`` 를 검증해 한 번 정한다. 실제 경로만 받는다(D-15).

    이름이 ``real_path`` 인 이유: 경로를 고쳐 주지 않고, 이미 실제 경로인지 확인만 한다.
    앞선 라운드처럼 ``abspath`` 로 ``..`` 를 접거나 부모 링크를 ``realpath`` 로 바꾸면, 다른
    파일을 가리키거나 Litestream 설정의 ``dbs[].path`` 와 어긋났다. 그래서
    - 원문에 ``..`` 구성요소가 있으면 거부한다.
    - ``normpath(abspath(raw))`` 의 마지막 구성요소가 ``.``·``..``·빈 문자열이면 거부한다.
    - 부모는 존재하는 디렉터리이고 ``realpath(parent, strict=True)`` 와 같아야 한다(부모 경로
      어디에도 링크·없는 구성요소가 없음).
    마지막 구성요소는 없어도 되고(새 DB), 그것이 링크인지는 따로 검사한다(DB 는 정규 파일만,
    격리 대상은 링크 거부). 이 경로를 잠금·격리·상태 파일과 Litestream 호출에 모두 쓴다.
    어긋나면 ``ValueError``(CLI 에서는 exit 64).
    """
    text = os.fspath(raw)
    hint = "use the real path; the litestream config dbs[].path must be the same real path (D-15)"
    if ".." in text.split(os.sep):
        raise ValueError(f"path must not contain '..': {text!r}; {hint}")
    if os.path.basename(text) in ("", ".", ".."):
        raise ValueError(f"path must end with a file name: {text!r}")
    path = os.path.normpath(os.path.abspath(text))
    if os.path.basename(path) in ("", ".", ".."):
        raise ValueError(f"path must end with a file name: {text!r}")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise ValueError(f"parent directory {parent} does not exist or is not a directory; {hint}")
    try:
        real = os.path.realpath(parent, strict=True)
    except OSError as exc:
        raise ValueError(f"cannot resolve {parent}: {exc}; {hint}") from None
    if real != parent:
        raise ValueError(
            f"parent directory {parent} is not a real path (it resolves to {real}); "
            f"pass {os.path.join(real, os.path.basename(path))} instead; {hint}"
        )
    return Path(path)


def _real_pair(db: Path, meta: Path) -> tuple[Path, Path]:
    """단계 함수를 직접 부를 때도 같은 계약을 지킨다. 어긋나면 exit 64."""
    try:
        return real_path(db), real_path(meta)
    except ValueError as exc:
        raise _Exit(EXIT_USAGE, str(exc)) from None


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def check_no_overlap(db: Path, meta: Path) -> None:
    """격리 대상끼리, 그리고 대상과 격리 목적지(DB 디렉터리 안의 .partial·final·임시 복원)가
    같은 경로이거나 한쪽이 다른 쪽의 조상이면 거부한다(exit 2). 예: 메타가 DB 의 부모면 DB 를
    옮긴 뒤 메타를 자기 안으로 옮기려다 실패하고, 재개도 같은 오류를 반복한다.
    """
    targets = quarantine_targets(db, meta)
    for i, a in enumerate(targets):
        for b in targets[i + 1 :]:
            if _overlaps(a, b):
                raise _Exit(EXIT_REFUSE, f"cannot quarantine: {a} and {b} overlap")
        if a == db.parent or a in db.parent.parents:
            raise _Exit(
                EXIT_REFUSE,
                f"cannot quarantine: {a} contains the quarantine destination {db.parent}",
            )


def lock_path(db: Path) -> Path:
    return db.with_name(db.name + ".boot.lock")


def state_path(db: Path) -> Path:
    return db.with_name(db.name + ".boot-state.json")


def sidecars(db: Path) -> list[Path]:
    return [db.with_name(db.name + s) for s in SIDECAR_SUFFIXES]


def quarantine_roles(db: Path, meta: Path) -> list[tuple[str, Path]]:
    """격리 대상의 (역할, 경로)와 순서. 이 목록 하나만 쓴다(격리·재개 검증·사이드카 판단 모두).

    DB 를 먼저 옮긴다. 도중에 죽으면 DB 는 이미 빠져 있으므로, 남은 사이드카·메타가 다음
    부팅에서 '진행 중 격리'로 이어서 옮겨진다. 메타는 마지막이다.
    """
    side = zip(("wal", "shm", "journal"), sidecars(db), strict=True)
    return [("db", db), *side, ("meta", meta)]


def quarantine_targets(db: Path, meta: Path) -> list[Path]:
    return [p for _, p in quarantine_roles(db, meta)]


def _timestamp() -> str:
    now = time.time_ns()
    base = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now // 1_000_000_000))
    return f"{base}.{now % 1_000_000_000 // 1000:06d}Z"


def _partial_re(db: Path) -> re.Pattern[str]:
    return re.compile(re.escape(db.name) + r"\.stale-([0-9T.Z]+)\.partial")


# --- 내구화 -------------------------------------------------------------------------------


def fsync_path(path: Path) -> None:
    """파일이나 디렉터리를 fsync 한다. rename 을 내구화하려면 부모 디렉터리를 fsync 한다."""
    flags = os.O_RDONLY
    if path.is_dir():
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dirs(*dirs: Path) -> None:
    seen: set[str] = set()
    for d in dirs:
        key = os.path.abspath(d)
        if key not in seen:
            seen.add(key)
            fsync_path(d)


# --- 1. 잠금 ------------------------------------------------------------------------------


def acquire_lock(db: Path) -> int:
    """``<db>.boot.lock`` 에 배타 잠금을 건 fd. exec 에 상속시켜 명령이 사는 동안 유지된다.

    잠금 파일은 정규 파일이어야 한다(링크·디렉터리·FIFO 거부). 잠근 뒤 경로가 아직 그 파일을
    가리키는지(inode) 확인한다. 이 검사는 획득 순간만 본다. 그 뒤 누가 잠금 파일을 지우거나
    바꾸면 flock 의 보호는 사라진다(DESIGN §4-2).
    """
    if fcntl is None:  # pragma: no cover - Windows
        raise _Exit(
            EXIT_USAGE,
            "file locking needs POSIX fcntl; boot is not verified on this platform (DESIGN §12)",
        )
    path = lock_path(db)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _Exit(EXIT_LOCK, f"cannot stat lock file {path}: {exc}") from None
    else:
        if not stat.S_ISREG(st.st_mode):
            raise _Exit(EXIT_LOCK, f"lock file {path} is not a regular file (or is a symlink)")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise _Exit(EXIT_LOCK, f"cannot open lock file {path}: {exc}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Exit(EXIT_LOCK, f"lock file {path} is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise _Exit(
                    EXIT_LOCK,
                    f"lock {path} is held by another boot or by the command it exec'd "
                    "(another process is using this volume)",
                ) from None
            raise _Exit(EXIT_LOCK, f"cannot lock {path}: {exc}") from None
        held = os.fstat(fd)
        try:
            now = os.lstat(path)
        except OSError:
            now = None
        if now is None or (now.st_dev, now.st_ino) != (held.st_dev, held.st_ino):
            raise _Exit(EXIT_LOCK, f"lock file {path} was replaced while locking")
    except BaseException:
        os.close(fd)
        raise
    return fd


# --- 2·5. 격리 ----------------------------------------------------------------------------
#
# manifest 는 "무엇을 옮기려 했는지"의 기록일 뿐 이동 명령이 아니다. 재개는 현재 DB(와
# --meta-path)에서 다시 유도한 대상과 대조해 일치할 때만, 전체를 먼저 검증한 뒤에 옮긴다.
# 이 검증은 사고·손상 대비이며, 쓰기 권한을 가진 공격자에 대한 인증이 아니다.

_MANIFEST_VERSION = 1


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}{_TMP_INFIX}{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, (json.dumps(data, indent=2) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    fsync_path(path.parent)


def _reserved(name: str) -> bool:
    """격리 디렉터리 안에서 우리가 쓰는 이름(manifest 와 그 임시 파일)."""
    return name == _MANIFEST or name.startswith(_MANIFEST + _TMP_INFIX)


def check_quarantinable(db: Path, meta: Path) -> None:
    """격리를 시작하기 전에(복원보다도 먼저) 옮길 수 있는지 본다. 아니면 exit 2.

    - 격리 대상(DB·사이드카·메타) 경로 자체가 심볼릭 링크면 거부한다. 링크를 rename 하면 실체는
      밖에 남고 격리본에는 끊어진 링크만 남는다. 링크 실체 보존은 v0.1 에서 지원하지 않는다.
      (판정·PROCEED·KEEP_LOCAL 에서 메타 디렉터리 링크를 따라가는 조회 규약과는 별개다.)
    - 대상 이름이 서로 겹치거나 격리 디렉터리의 예약 이름(manifest)과 겹치면 거부한다.
    - rename 은 같은 파일시스템 안에서만 원자적이다. 다른 파일시스템이면 거부한다.
    """
    db, meta = _real_pair(db, meta)
    check_no_overlap(db, meta)
    names = [p.name for _, p in quarantine_roles(db, meta)]
    if len(set(names)) != len(names):
        raise _Exit(EXIT_REFUSE, f"cannot quarantine: duplicate target names {names}")
    clash = [n for n in names if _reserved(n)]
    if clash:
        raise _Exit(
            EXIT_REFUSE,
            f"cannot quarantine: {clash} collide with the reserved name {_MANIFEST!r}; "
            "rename the db or --meta-path",
        )
    try:
        dev = os.stat(db.parent).st_dev
        for _, p in quarantine_roles(db, meta):
            if not os.path.lexists(p):
                continue
            if os.path.islink(p):
                raise _Exit(
                    EXIT_REFUSE,
                    f"cannot quarantine {p}: it is a symbolic link; boot does not move link "
                    "targets (v0.1). Replace the link with the real file or directory",
                )
            if os.stat(p.parent).st_dev != dev:
                raise _Exit(
                    EXIT_REFUSE,
                    f"cannot quarantine {p}: not on the same filesystem as {db.parent}",
                )
    except OSError as exc:
        raise _Exit(EXIT_REFUSE, f"cannot quarantine: {exc}") from None


def _move_all(partial: Path, entries: list[tuple[str, Path, str]]) -> None:
    """검증이 끝난 목록에서 아직 원래 자리에 있는 대상을 순서대로 ``partial`` 안으로 옮긴다."""
    for _, src, name in entries:
        if os.path.lexists(src):
            os.rename(src, partial / name)
            log(f"quarantine: moved {src} -> {partial / name}")


def _finish(partial: Path, final: Path, entries: list[tuple[str, Path, str]]) -> None:
    _move_all(partial, entries)
    _fsync_dirs(partial, partial.parent, *(src.parent for _, src, _ in entries))
    os.rename(partial, final)
    fsync_path(final.parent)
    log(f"quarantine: done -> {final}")


def quarantine(db: Path, meta: Path) -> Path | None:
    """있는 격리 대상을 ``<db>.stale-<ts>/`` 로 옮긴다. 옮길 것이 없으면 ``None``.

    먼저 ``.partial`` 디렉터리와 manifest(대상 DB, 항목마다 역할·원래 경로·이름, 순서)를
    내구화한 뒤 옮긴다.
    """
    db, meta = _real_pair(db, meta)
    check_quarantinable(db, meta)
    present = [(role, p) for role, p in quarantine_roles(db, meta) if os.path.lexists(p)]
    if not present:
        return None
    while True:
        ts = _timestamp()
        partial = db.with_name(f"{db.name}.stale-{ts}.partial")
        final = db.with_name(f"{db.name}.stale-{ts}")
        if os.path.lexists(final):
            continue
        try:
            os.mkdir(partial, 0o755)
        except FileExistsError:
            continue
        break
    fsync_path(partial.parent)
    entries = [(role, p, p.name) for role, p in present]
    manifest = {
        "version": _MANIFEST_VERSION,
        "db": str(db),
        "entries": [{"role": r, "src": str(src), "name": n} for r, src, n in entries],
    }
    _write_json_atomic(partial / _MANIFEST, manifest)
    log(f"quarantine: started {partial} ({', '.join(n for _, _, n in entries)})")
    _finish(partial, final, entries)
    return final


def _refuse_resume(partial: Path, why: str) -> _Exit:
    return _Exit(EXIT_REFUSE, f"cannot resume interrupted quarantine {partial}: {why}")


def _load_manifest(db: Path, meta: Path, partial: Path) -> list[tuple[str, Path, str]]:
    """manifest 를 읽어 현재 DB·메타에서 유도한 대상과 대조한다. 어긋나면 exit 2."""
    manifest = partial / _MANIFEST
    if not stat.S_ISREG(os.lstat(manifest).st_mode):
        raise _refuse_resume(partial, "manifest.json is not a regular file (or is a symlink)")
    try:
        data = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise _refuse_resume(partial, f"cannot read manifest: {exc}") from None
    if (
        type(data) is not dict
        or type(data.get("version")) is not int
        or data["version"] != _MANIFEST_VERSION
        or type(data.get("db")) is not str
        or type(data.get("entries")) is not list
    ):
        raise _refuse_resume(partial, "manifest schema mismatch")
    if data["db"] != str(db):
        raise _refuse_resume(partial, f"manifest is for db {data['db']!r}, not {db}")

    expected = dict(quarantine_roles(db, meta))
    order = list(expected)
    entries: list[tuple[str, Path, str]] = []
    for item in data["entries"]:
        if type(item) is not dict or set(item) != {"role", "src", "name"}:
            raise _refuse_resume(partial, "manifest entry schema mismatch")
        role, src, name = item["role"], item["src"], item["name"]
        if not all(type(v) is str for v in (role, src, name)) or role not in expected:
            raise _refuse_resume(partial, f"manifest entry has invalid role {role!r}")
        if src != str(expected[role]):
            if role == "meta":
                raise _refuse_resume(
                    partial,
                    f"meta path in manifest {src!r} differs from --meta-path {expected[role]}; "
                    "rerun with the original --meta-path",
                )
            raise _refuse_resume(partial, f"manifest entry {role} has unexpected src {src!r}")
        if name != expected[role].name or _reserved(name):
            raise _refuse_resume(partial, f"manifest entry {role} has invalid name {name!r}")
        entries.append((role, expected[role], name))
    roles = [r for r, _, _ in entries]
    if len(set(roles)) != len(roles) or roles != sorted(roles, key=order.index):
        raise _refuse_resume(partial, "manifest entries are duplicated or out of order")
    return entries


def resume_quarantine(db: Path, meta: Path) -> None:
    """앞선 부팅이 격리 도중 죽었으면(``.partial`` 이 남음) 판정 전에 끝낸다.

    전체를 먼저 검증하고, 하나라도 어긋나면 아무것도 옮기지 않고 exit 2 다. 둘 이상이면 거부.
    """
    db, meta = _real_pair(db, meta)
    pattern = _partial_re(db)
    try:
        found = sorted(n for n in os.listdir(db.parent) if pattern.fullmatch(n))
    except OSError as exc:
        raise _Exit(EXIT_REFUSE, f"cannot list {db.parent}: {exc}") from None
    if not found:
        return
    if len(found) > 1:
        raise _Exit(
            EXIT_REFUSE,
            f"more than one interrupted quarantine in {db.parent}: {', '.join(found)}; "
            "inspect and merge manually",
        )
    partial = db.parent / found[0]
    final = db.with_name(found[0].removesuffix(".partial"))
    check_no_overlap(db, meta)
    if not stat.S_ISDIR(os.lstat(partial).st_mode):
        raise _refuse_resume(partial, "it is not a directory (or is a symlink)")
    contents = set(os.listdir(partial))
    if _MANIFEST not in contents:
        # manifest 를 쓰기 전에 죽었다. manifest 는 무엇보다 먼저 쓰므로 옮긴 파일이 없다.
        leftovers = sorted(n for n in contents if not _reserved(n))
        if leftovers:
            raise _refuse_resume(partial, f"no manifest but contains {leftovers}")
        shutil.rmtree(partial)
        fsync_path(partial.parent)
        log(f"quarantine: removed {partial} (interrupted before any file was moved)")
        return
    entries = _load_manifest(db, meta, partial)
    unknown = sorted(contents - {_MANIFEST} - {n for _, _, n in entries})
    if unknown:
        raise _refuse_resume(partial, f"unexpected files {unknown}")
    # manifest 에 없는 역할의 파일이 어디에든 있으면 거부한다. 자동으로 보충하지 않는다.
    # (예: WAL 항목이 빠진 manifest 로 DB 만 격리하면 DB 와 WAL 이 갈라진다.)
    listed = {role for role, _, _ in entries}
    for role, path in quarantine_roles(db, meta):
        if role in listed:
            continue
        for where in (path, partial / path.name):
            if os.path.lexists(where):
                raise _refuse_resume(partial, f"{role} {where} exists but is not in the manifest")
    for role, src, name in entries:
        here, there = os.path.lexists(src), os.path.lexists(partial / name)
        if here == there:
            where = "both the original place and" if here else "neither the original place nor"
            raise _refuse_resume(partial, f"{role} {name} is in {where} the quarantine")
        if os.path.islink(src if here else partial / name):
            raise _refuse_resume(partial, f"{role} {name} is a symbolic link")
    if os.path.lexists(final):
        raise _refuse_resume(partial, f"{final} already exists")
    log(f"quarantine: resuming interrupted {partial}")
    _finish(partial, final, entries)


# --- 3. 판정 입력 -------------------------------------------------------------------------


def local_db_exists(db: Path) -> bool:
    """DB 경로가 정규 파일이면 참, 없으면 거짓. 그 밖의 것(디렉터리·링크 등)이면 거부."""
    try:
        mode = os.lstat(db).st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _Exit(EXIT_REFUSE, f"cannot stat {db}: {exc}") from None
    if not stat.S_ISREG(mode):
        raise _Exit(EXIT_REFUSE, f"{db} exists but is not a regular file")
    return True


# --- 5. 임시 복원·설치 -----------------------------------------------------------------------


def restore_to_temp(db: Path, args: argparse.Namespace) -> tuple[Path, int]:
    """DB 와 같은 디렉터리의 새 ``<db>.restore-<ts>-<rand>/`` 로 복원한다. 실패하면 exit 4.

    실패한 임시 디렉터리는 조사용으로 남긴다. 다음 부팅은 이 디렉터리를 판정에 쓰지 않는다.
    """
    tmpdir = db.with_name(f"{db.name}.restore-{_timestamp()}-{secrets.token_hex(4)}")
    try:
        os.mkdir(tmpdir, 0o755)
    except OSError as exc:
        raise _Exit(EXIT_RESTORE, f"cannot create restore directory {tmpdir}: {exc}") from None
    out = tmpdir / db.name
    log(f"restore: {db} -> {out}")
    result = ls.restore(
        db,
        out,
        config=args.config,
        binary=args.litestream,
        timeout=args.restore_timeout,
    )
    if not result.ok:
        raise _Exit(EXIT_RESTORE, f"restore failed: {result.reason} (left {tmpdir} for inspection)")
    extra = [n for n in os.listdir(tmpdir) if n != db.name and n != db.name + "-shm"]
    if extra:
        raise _Exit(
            EXIT_RESTORE,
            f"restore left unexpected files {sorted(extra)} in {tmpdir}; not installing",
        )
    assert result.txid is not None
    log(f"restore: ok, txid {result.txid}")
    return out, result.txid


def install(restored: Path, db: Path) -> None:
    """임시 복원 DB 를 DB 경로로 rename 한다. 그 자리에 무엇이 있으면 거부한다."""
    if os.path.lexists(db):
        raise _Exit(EXIT_REFUSE, f"cannot install restored db: {db} exists")
    for side in sidecars(db):
        if os.path.lexists(side):
            raise _Exit(EXIT_REFUSE, f"cannot install restored db: {side} exists")
    fsync_path(restored)
    fsync_path(restored.parent)
    os.rename(restored, db)
    fsync_path(db.parent)
    shutil.rmtree(restored.parent)
    log(f"install: {db}")


# --- 6. 무결성 ----------------------------------------------------------------------------


def integrity_problem(db: Path) -> str | None:
    """읽기 전용 URI 로 ``PRAGMA quick_check``. 정상이면 ``None``, 아니면 사유."""
    uri = f"file:{quote(os.path.abspath(db))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"cannot check {db}: {exc}"
    if rows != [("ok",)]:
        return f"quick_check failed: {' | '.join(str(r[0]) for r in rows[:5])}"
    return None


# --- 7. 상태 파일 -------------------------------------------------------------------------


def write_state(db: Path, plan: "_Plan", *, litestream_version: str | None) -> Path:
    path = state_path(db)
    _write_json_atomic(
        path,
        {
            "version": _STATE_VERSION,
            "state": plan.state,
            "action": plan.action,
            "reason_code": plan.reason_code,
            "reason": plan.reason,
            "unknown_at_boot": plan.action == Action.KEEP_LOCAL,
            "litestream_version": litestream_version,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return path


# --- 인자 ---------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="python -m django_sqlite_ops.boot",
        description=(
            "Decide whether the local SQLite DB can be used, restore it from Litestream if "
            "needed, then exec the command after '--'. Refuses to start when it cannot decide."
        ),
        epilog="exit codes: 2 refused, 3 integrity, 4 restore failed, 5 lock, 64 usage, "
        "127 exec failed",
    )
    p.add_argument("--db", required=True, help="SQLite database path")
    p.add_argument("--config", required=True, type=Path, help="litestream config path")
    p.add_argument(
        "--on-unknown",
        choices=[o.value for o in OnUnknown],
        default=OnUnknown.REFUSE.value,
        help="what to do when the state is unknown (default: refuse)",
    )
    p.add_argument(
        "--adopt-existing",
        action="store_true",
        help="first deploy of an existing db to litestream; turn off afterwards (D-11)",
    )
    p.add_argument(
        "--init-new",
        action="store_true",
        help="first deploy ever: start a new db on an empty replica; turn off afterwards (D-13)",
    )
    p.add_argument("--meta-path", help="litestream meta dir (default: .<db>-litestream)")
    p.add_argument("--litestream", default="litestream", help="litestream binary")
    p.add_argument("--ltx-timeout", type=_seconds, default=ls.DEFAULT_LTX_TIMEOUT)
    p.add_argument("--restore-timeout", type=_seconds, default=ls.DEFAULT_RESTORE_TIMEOUT)
    return p


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = build_parser()
    if "--" in argv:
        i = argv.index("--")
        own, command = argv[:i], argv[i + 1 :]
    else:
        own, command = argv, []
    args = parser.parse_args(own)
    if not command:
        parser.error("missing command after '--'")
    # 잠금·사이드카·임시 복원·격리·설치·상태 파일과 Litestream 호출이 모두 이 경로를 쓴다.
    try:
        args.db = real_path(args.db)
        meta = ls.default_meta_path(args.db) if args.meta_path is None else args.meta_path
        args.meta_path = real_path(meta)
    except ValueError as exc:
        parser.error(str(exc))
    return args, command


# --- 판정과 조치 ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    state: str
    action: str
    reason_code: str
    reason: str


_HINTS = {
    ReasonCode.NO_REPLICA_NO_LOCAL: (
        "if this is the very first deploy, run once with --init-new and remove it afterwards "
        "(D-13); otherwise check the replica path/prefix in the litestream config"
    ),
    ReasonCode.REMOTE_ERROR: (
        "fix replica access (endpoint needs http:// for plain HTTP, credentials, network) and "
        "retry; boot refuses on lookup failure regardless of --on-unknown (D-12)"
    ),
    ReasonCode.NO_LOCAL_META: (
        "if the previous boot restored this db and litestream died before writing its first "
        "L0 (D-14), rerun once with --on-unknown restore; to put an existing db on an empty "
        "replica for the first time, use --adopt-existing once (D-11)"
    ),
    ReasonCode.STALE_META: (
        "litestream meta exists without the db; rerun with --on-unknown restore to quarantine "
        "it and restore from the replica"
    ),
    ReasonCode.REMOTE_EMPTY: (
        "local db has replicated before but the replica is empty: check the replica path/prefix; "
        "to keep the local db anyway use --on-unknown keep-local"
    ),
    ReasonCode.REMOTE_AHEAD: (
        "the replica is newer than this volume (another host wrote after it); rerun with "
        "--on-unknown restore to quarantine local files and restore"
    ),
    ORPHAN_SIDECARS: (
        "-wal/-shm/-journal exist without the db file; inspect them, or rerun with "
        "--on-unknown restore to quarantine them and restore from the replica"
    ),
}


def _plan(args: argparse.Namespace) -> _Plan:
    db, meta = args.db, args.meta_path
    local_exists = local_db_exists(db)
    local_txid = ls.local_max_txid(db, meta_path=meta)
    remote = ls.remote_max_txid(
        db, config=args.config, binary=args.litestream, timeout=args.ltx_timeout
    )
    if type(remote) is RemoteTxid:
        remote_text = f"txid {remote.txid}"
    elif type(remote) is RemoteError:
        remote_text = f"error: {remote.message}"
    else:
        remote_text = "empty"
    log(f"inputs: local_exists={local_exists} local_txid={local_txid} remote={remote_text}")

    orphans = [p.name for p in sidecars(db) if os.path.lexists(p)]
    if not local_exists and orphans:
        reason = f"no local db but {', '.join(orphans)} exist; remote {remote_text}"
        if args.on_unknown == OnUnknown.RESTORE.value and type(remote) is RemoteTxid:
            return _Plan(State.UNKNOWN, Action.QUARANTINE_AND_RESTORE, ORPHAN_SIDECARS, reason)
        return _Plan(State.UNKNOWN, Action.REFUSE, ORPHAN_SIDECARS, reason)

    d = decide(
        local_exists=local_exists,
        local_txid=local_txid,
        remote=remote,
        on_unknown=args.on_unknown,
        adopt_existing=args.adopt_existing,
        init_new=args.init_new,
    )
    return _Plan(d.state, d.action, d.reason_code, d.reason)


def _act(args: argparse.Namespace, plan: _Plan) -> None:
    db, meta = args.db, args.meta_path
    if plan.action == Action.REFUSE:
        hint = _HINTS.get(plan.reason_code)
        if hint:
            log(f"hint: {hint}")
        raise _Exit(EXIT_REFUSE, f"refused ({plan.reason_code}): {plan.reason}")
    if plan.action in (Action.RESTORE, Action.QUARANTINE_AND_RESTORE):
        # 복원·검증을 먼저 끝낸다. 실패하면 로컬은 그대로다(L7).
        check_quarantinable(db, meta)
        restored, _ = restore_to_temp(db, args)
        stale = quarantine(db, meta)
        if stale is None and plan.action == Action.QUARANTINE_AND_RESTORE:
            log("quarantine: nothing to move")
        install(restored, db)
        return
    if plan.action == Action.KEEP_LOCAL:
        log(f"WARNING: unknown_at_boot ({plan.reason_code}); keeping local db as requested")
        return
    assert plan.action == Action.PROCEED
    if plan.state == State.ADOPT:
        log("note: turn off --adopt-existing after this first deploy (D-11)")
    elif plan.reason_code == ReasonCode.NEW_DB:
        log("note: new db will be created by the app; turn off --init-new after this deploy (D-13)")


def run(args: argparse.Namespace, command: list[str]) -> int:
    """잠금부터 exec 까지. exec 에 성공하면 돌아오지 않는다(잠금은 exec 된 명령이 쥔다)."""
    lock_fd = acquire_lock(args.db)
    try:
        return _run_locked(args, command, lock_fd)
    finally:
        # exec 에 성공하면 여기 오지 않는다. 실패·거부로 돌아올 때만 잠금을 푼다.
        os.close(lock_fd)


def _run_locked(args: argparse.Namespace, command: list[str], lock_fd: int) -> int:
    db = args.db
    log(f"lock: {lock_path(db)}")
    try:
        resume_quarantine(db, args.meta_path)
    except OSError as exc:
        raise _Exit(EXIT_REFUSE, f"cannot finish interrupted quarantine: {exc}") from None

    plan = _plan(args)
    log(
        f"decision: state={plan.state} action={plan.action} "
        f"reason_code={plan.reason_code} reason={plan.reason}"
    )
    try:
        _act(args, plan)
    except OSError as exc:
        # 격리·설치 도중의 파일 오류. 격리가 반쯤이면 .partial 이 남고 다음 부팅이 이어서 끝낸다.
        raise _Exit(EXIT_RESTORE, f"restore/quarantine step failed: {exc}") from None

    if local_db_exists(db):
        problem = integrity_problem(db)
        if problem is not None:
            raise _Exit(EXIT_INTEGRITY, f"integrity: {problem}")
        log("integrity: quick_check ok")
    else:
        log("integrity: skipped (no db file yet; the app creates it)")

    try:
        path = write_state(db, plan, litestream_version=ls.version(binary=args.litestream))
    except OSError as exc:
        raise _Exit(EXIT_REFUSE, f"cannot write boot state: {exc}") from None
    log(f"state: {path}")

    # 잠금을 exec 된 명령에 넘긴다. 그 명령(과 그 자식)이 살아 있는 동안 두 번째 boot 가 막힌다.
    os.set_inheritable(lock_fd, True)
    log(f"exec: {' '.join(command)}")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        raise _Exit(EXIT_EXEC, f"cannot exec {command[0]}: {exc}") from None
    raise AssertionError("unreachable")  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    args, command = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(args, command)
    except _Exit as exc:
        log(f"exit {exc.code}: {exc.message}")
        return exc.code
