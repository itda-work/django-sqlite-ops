"""``manage.py sqlite_doctor`` 의 진단 로직 (DESIGN §6-2).

정적 체크(§6-1)와 달리 DB 에 실제로 연결한다. 연결하면 Django 가 ``init_command`` 를 실행하므로
진단이 DB 상태를 바꿀 수 있다(예: ``PRAGMA journal_mode=WAL`` 은 파일에 남는다). 그래서 명시적으로
실행하는 명령으로만 둔다. **DB 파일이 없으면 연결하지 않는다.** 연결하면 빈 DB 가 생겨 boot 의
복원 판정을 망친다(DESIGN §4-1).

항목마다 수준은 ``ok`` · ``warn`` · ``error`` · ``unknown`` 이다. 판정할 수 없으면 숨기지 않고
``unknown`` 으로 적고, 종료 코드에서는 경고로 센다.
"""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings
from django.db import connections

from .boot import litestream
from .checks import _is_litestream_vfs, _profile, _role, _uri
from .database import DEFAULT_PROFILE, ENGINE, recommended

__all__ = [
    "LEVELS",
    "SCHEMA_VERSION",
    "Item",
    "classify_fstype",
    "diagnose",
    "exit_code",
    "filesystem_type",
    "format_text",
    "mountinfo_fstype",
    "mount_output_fstype",
    "to_json",
]

SCHEMA_VERSION = 1
LEVELS = ("ok", "warn", "error", "unknown")
SECTIONS = ("settings", "database", "mount", "litestream", "channels")

# PRAGMA 값 매핑의 정본. SQLite 는 synchronous 를 정수로 돌려준다(https://www.sqlite.org/pragma.html).
_SYNCHRONOUS = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}
# 읽기 전용 별칭에는 의미가 없거나 따르면 연결이 깨지는 쓰기 권고(정적 체크 W001·W002 와 같은 원칙).
_WRITE_KEYS = frozenset({"transaction_mode", "journal_mode", "synchronous"})

_NETWORK_FS_DOC = "https://www.sqlite.org/useovernet.html"
# 네트워크 파일시스템. SQLite 의 파일 잠금을 믿을 수 없고 WAL 은 동작하지 않는다.
_NETWORK_FS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb",
        "smb2",
        "smb3",
        "smbfs",
        "afpfs",
        "webdav",
        "davfs",
        "fuse.davfs",
        "sshfs",
        "fuse.sshfs",
        "9p",
        "ceph",
        "fuse.ceph",
        "glusterfs",
        "fuse.glusterfs",
        "lustre",
        "gpfs",
        "fuse.s3fs",
        "fuse.rclone",
        "fuse.gcsfuse",
        "fuse.juicefs",
        "efs",
    }
)
# 로컬 디스크·메모리 파일시스템. 여기에도 네트워크에도 없으면 판정하지 않는다(unknown).
_LOCAL_FS = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "zfs",
        "f2fs",
        "jfs",
        "reiserfs",
        "bcachefs",
        "overlay",
        "tmpfs",
        "ramfs",
        "apfs",
        "hfs",
        "ufs",
        "ffs",
        "msdos",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
    }
)


@dataclass(frozen=True, slots=True)
class Item:
    """진단 항목 하나. ``--json`` 의 ``items`` 배열 원소와 같은 모양이다(DESIGN §6-2)."""

    section: str
    alias: str | None
    key: str
    level: str
    value: Any = None
    expected: Any = None
    message: str = ""


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


# --- 마운트 -----------------------------------------------------------------------------


def classify_fstype(fstype: str | None) -> tuple[str, str]:
    """파일시스템 종류를 ``(수준, 사유)`` 로 판정한다."""
    if not fstype:
        return "unknown", "cannot determine the filesystem type"
    name = fstype.lower()
    if name in _NETWORK_FS:
        return (
            "warn",
            f"network filesystem: SQLite file locking is unreliable and WAL does not work "
            f"over a network filesystem ({_NETWORK_FS_DOC})",
        )
    if name in _LOCAL_FS:
        return "ok", "local filesystem"
    return "unknown", f"filesystem type {fstype!r} is not known to be local or network"


def _unescape_mountinfo(field: str) -> str:
    # 공백·탭·줄바꿈·역슬래시는 8진수 이스케이프로 나온다(proc(5) mountinfo).
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def _under(path: str, mount_point: str) -> bool:
    if mount_point == "/":
        return path.startswith("/")
    return path == mount_point or path.startswith(mount_point.rstrip("/") + "/")


def mountinfo_fstype(text: str, path: str) -> str | None:
    """``/proc/self/mountinfo`` 텍스트에서 ``path`` 가 속한 마운트의 종류를 찾는다.

    가장 긴 마운트 지점이 이긴다. 같은 지점에 여러 번 마운트됐으면 나중 줄(위에 덮인 것)이 이긴다.
    ``path`` 는 실제 경로(링크를 푼 절대 경로)여야 한다.
    """
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split()
        rest = right.split()
        if len(fields) < 5 or not rest:
            continue
        mount_point = _unescape_mountinfo(fields[4])
        if _under(path, mount_point) and (best is None or len(mount_point) >= best[0]):
            best = (len(mount_point), rest[0])
    return best[1] if best else None


def mount_output_fstype(text: str, path: str) -> str | None:
    """BSD·macOS ``mount`` 출력(``<장치> on <지점> (<종류>, ...)``)에서 종류를 찾는다."""
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        head, sep, tail = line.rpartition(" (")
        if not sep or " on " not in head:
            continue
        mount_point = head.split(" on ", 1)[1]
        fstype = tail.split(",", 1)[0].rstrip(")").strip()
        if fstype and _under(path, mount_point) and (best is None or len(mount_point) >= best[0]):
            best = (len(mount_point), fstype)
    return best[1] if best else None


class _MacStatfs(ctypes.Structure):
    # <sys/mount.h> 의 64비트 inode struct statfs (macOS).
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _mac_statfs(path: str) -> str | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # x86_64 는 64비트 inode 판이 $INODE64 접미사를 단다. arm64 는 접미사가 없다.
        func = getattr(libc, "statfs$INODE64", None) or libc.statfs
        func.argtypes = [ctypes.c_char_p, ctypes.POINTER(_MacStatfs)]
        buf = _MacStatfs()
        if func(os.fsencode(path), ctypes.byref(buf)) != 0:
            return None
        fstype = buf.f_fstypename.decode("utf-8", errors="replace")
        mount_point = buf.f_mntonname.decode("utf-8", errors="replace")
    except (OSError, AttributeError, ValueError):
        return None
    # 구조체 배치가 어긋났으면 지점이 경로의 앞부분이 아니다. 그럴 때는 믿지 않는다.
    if not fstype or not mount_point or not _under(path, mount_point):
        return None
    return fstype


def _existing(path: str) -> str:
    """``path`` 또는 그 가장 가까운 존재하는 상위 디렉터리의 실제 경로."""
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.realpath(current)


def filesystem_type(path: str) -> str | None:
    """``path``(없으면 존재하는 상위 디렉터리)의 파일시스템 종류. 모르면 ``None``."""
    target = _existing(path)
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
                return mountinfo_fstype(f.read(), target)
        except OSError:
            return None
    if sys.platform == "darwin":
        fstype = _mac_statfs(target)
        if fstype:
            return fstype
    if os.name != "posix":
        return None
    try:
        run = subprocess.run(["mount"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    return mount_output_fstype(run.stdout, target)


# --- DB 별칭 ----------------------------------------------------------------------------


def _db_file(name: Any) -> str | None:
    """별칭 ``NAME`` 의 파일 경로(URI 면 디코딩한 파일명). 판정할 수 없으면 ``None``."""
    if isinstance(name, os.PathLike):
        name = os.fspath(name)
    if not isinstance(name, str) or not name:
        return None
    uri = _uri(name)
    path = uri[0] if uri is not None else name
    return path or None


def _alias_role(name: Any) -> tuple[str, str | None]:
    """``checks._role()`` 과 같되, ``vfs=litestream`` 이면 ``mode=ro`` 가 함께 있어도 VFS 다.

    정적 체크는 읽기 전용과 VFS 를 똑같이 건너뛰므로 순서가 상관없지만, doctor 는 VFS 에
    연결하지 않아야 한다(확장이 필요하다).
    """
    role, reason = _role(name)
    if role == "read-only" and _is_litestream_vfs(name):
        return "vfs", None
    return role, reason


def _canon(key: str, value: Any) -> Any:
    """비교용 정규화. 실제 값과 권장값을 같은 표기로 맞춘다."""
    if key == "synchronous":
        if isinstance(value, int) and not isinstance(value, bool):
            return _SYNCHRONOUS.get(value, value)
        return str(value).upper()
    if key in ("journal_mode", "transaction_mode"):
        return None if value is None else str(value).upper()
    if isinstance(value, bool):
        return int(value)
    return value


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _read_pragmas(alias: str, keys: Iterable[str]) -> dict[str, Any]:
    conn = connections[alias]
    try:
        conn.ensure_connection()  # init_command 가 여기서 실행된다
        values: dict[str, Any] = {"transaction_mode": getattr(conn, "transaction_mode", None)}
        with conn.cursor() as cursor:
            for key in keys:
                cursor.execute(f"PRAGMA {key}")
                row = cursor.fetchone()
                values[key] = row[0] if row else None
            cursor.execute("SELECT sqlite_version()")
            values["sqlite_version"] = cursor.fetchone()[0]
        return values
    finally:
        conn.close()


def _diagnose_alias(alias: str, config: Mapping[str, Any], rec: dict[str, Any]) -> list[Item]:
    section = "database"
    name = config.get("NAME")
    role, reason = _alias_role(name)
    items = [Item(section, alias, "role", "ok", role)]
    if role == "unknown":
        return [Item(section, alias, "role", "unknown", role, None, f"not connected: {reason}")]
    if role == "vfs":
        return [
            Item(
                section,
                alias,
                "role",
                "unknown",
                role,
                None,
                "not connected: the Litestream VFS needs its extension loaded",
            )
        ]
    path = None
    if role != "memory":
        path = _db_file(name)
        if path is None:
            return [Item(section, alias, "file", "error", None, None, "NAME is empty")]
        if not os.path.exists(path):
            return items + [
                Item(
                    section,
                    alias,
                    "file",
                    "error",
                    os.path.abspath(path),
                    None,
                    "database file does not exist; not connected (connecting would create "
                    "an empty database)",
                )
            ]
        if not os.path.isfile(path):
            return items + [
                Item(
                    section,
                    alias,
                    "file",
                    "error",
                    os.path.abspath(path),
                    None,
                    "not a regular file; not connected",
                )
            ]
        items.append(Item(section, alias, "file", "ok", _file_size(path), None, "bytes"))
        wal = _file_size(path + "-wal")
        items.append(
            Item(
                section,
                alias,
                "wal",
                "ok",
                wal,
                None,
                "bytes" if wal is not None else "no -wal file",
            )
        )
    pragmas = rec["pragmas"]
    keys = list(dict.fromkeys([*pragmas, "foreign_keys"]))
    try:
        values = _read_pragmas(alias, keys)
    except Exception as exc:  # noqa: BLE001 - 어떤 연결 오류든 한 줄로 보고한다
        return items + [
            Item(
                section,
                alias,
                "connect",
                "error",
                None,
                None,
                _one_line(f"{type(exc).__name__}: {exc}"),
            )
        ]
    if role == "memory":
        return [Item(section, alias, "role", "ok", role, None, "in-memory database")]
    expected = {"transaction_mode": rec["transaction_mode"], **pragmas}
    for key in ["transaction_mode", *keys]:
        actual = _canon(key, values.get(key))
        if key not in expected:
            items.append(Item(section, alias, key, "ok", actual))
        elif role == "read-only" and key in _WRITE_KEYS:
            items.append(Item(section, alias, key, "ok", actual, None, "read-only: not compared"))
        else:
            want = _canon(key, expected[key])
            level = "ok" if actual == want else "warn"
            message = "" if level == "ok" else "differs from the recommended value (DESIGN §6-0)"
            items.append(Item(section, alias, key, level, actual, want, message))
    items.append(Item(section, alias, "sqlite_version", "ok", values["sqlite_version"]))
    return items


def _diagnose_mount(alias: str, path: str) -> Item:
    fstype = filesystem_type(path)
    level, message = classify_fstype(fstype)
    return Item("mount", alias, "filesystem", level, fstype, None, message)


# --- Litestream --------------------------------------------------------------------------


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _litestream_items(
    config: str,
    binary: str,
    db_paths: Mapping[str, str],
    write_aliases: Iterable[str],
) -> tuple[list[Item], set[str] | None]:
    """설정의 DB 목록을 ``DATABASES`` 와 대조한다.

    ``(항목, 설정 DB 의 실제 경로 집합 또는 None)`` 을 돌려준다.
    """
    section = "litestream"
    # 상대 경로·dir: 항목은 Litestream 작업 디렉터리 기준으로 풀린다. 새 임시 디렉터리에서 돌려
    # 그 아래로 풀린 항목을 골라낸다(사이드카의 작업 디렉터리를 doctor 는 알 수 없다).
    with tempfile.TemporaryDirectory(prefix="sqlite-doctor-") as cwd:
        result = litestream.config_databases(config, binary=binary, cwd=cwd)
        cwd_forms = {cwd, os.path.realpath(cwd)}
    if isinstance(result, str):
        return [Item(section, None, "config", "error", config, None, result)], None
    items = [Item(section, None, "config", "ok", config, None, f"{len(result)} database(s)")]
    by_real: dict[str, str] = {}
    for path in result:
        if any(path == c or path.startswith(c + os.sep) for c in cwd_forms):
            items.append(
                Item(
                    section,
                    None,
                    "config_path",
                    "warn",
                    path,
                    None,
                    "relative path or dir: entry; it resolves against litestream's working "
                    "directory and cannot be matched to DATABASES",
                )
            )
            continue
        real = os.path.realpath(path)
        if real != path:
            items.append(
                Item(
                    section,
                    None,
                    "config_path",
                    "warn",
                    path,
                    real,
                    "not a real path (symlink or '..'); boot refuses it — use the real path (D-15)",
                )
            )
        by_real[real] = path
    alias_by_real: dict[str, str] = {}
    for alias, path in db_paths.items():
        alias_by_real.setdefault(_real(path), alias)
    for alias in write_aliases:
        real = _real(db_paths[alias])
        if real in by_real:
            items.append(
                Item(section, alias, "replicated", "ok", by_real[real], None, "in the config")
            )
        else:
            items.append(
                Item(
                    section,
                    alias,
                    "replicated",
                    "warn",
                    None,
                    real,
                    "write database is not in the litestream config; it is not replicated",
                )
            )
    for real, path in by_real.items():
        if real not in alias_by_real:
            items.append(
                Item(section, None, "extra", "ok", path, None, "in the config but not in DATABASES")
            )
    return items, set(by_real)


# --- 채널 레이어 -------------------------------------------------------------------------

_INMEMORY = "channels.layers.InMemoryChannelLayer"
_NATS = ("channels_nats.NatsChannelLayer", "channels_nats.layer.NatsChannelLayer")
_REDIS = "channels_redis.core.RedisChannelLayer"
_REDIS_PUBSUB = "channels_redis.pubsub.RedisPubSubChannelLayer"
_LITE = (
    "channels_lite.layers.core.SQLiteChannelLayer",
    "channels_lite.layers.aio.AIOSQLiteChannelLayer",
)

# DESIGN §8 표의 요약.
_SUMMARY = {
    _INMEMORY: "in-memory: one process only; messages do not reach other processes or workers",
    _NATS: (
        "channels-nats: messages sent before a receiver exists are dropped; ordering only "
        "within one subscription; no ChannelFull; no disk writes"
    ),
    _REDIS: (
        "channels_redis: messages are kept until expiry; per-channel ordering; ChannelFull "
        "can be raised; no SQLite writes"
    ),
    _LITE: (
        "channels-lite: not recommended — polling trades latency for idle CPU and every "
        "message costs 2 DB writes; keep it in a separate file excluded from Litestream"
    ),
}


def _channel_items(
    sqlite_files: Mapping[str, str],
    replicated: set[str] | None,
    profile: str,
) -> list[Item]:
    section = "channels"
    layers = getattr(settings, "CHANNEL_LAYERS", None)
    if layers is None:
        return [Item(section, None, "backend", "ok", None, None, "CHANNEL_LAYERS is not set")]
    if not isinstance(layers, Mapping):
        return [
            Item(section, None, "backend", "unknown", None, None, "CHANNEL_LAYERS is not a dict")
        ]
    items = []
    for name, layer in layers.items():
        backend = layer.get("BACKEND") if isinstance(layer, Mapping) else None
        if not isinstance(backend, str):
            items.append(Item(section, name, "backend", "unknown", None, None, "no BACKEND"))
            continue
        if backend == _INMEMORY:
            if profile == "single-server-multiproc":
                items.append(
                    Item(
                        section,
                        name,
                        "backend",
                        "warn",
                        backend,
                        None,
                        _SUMMARY[_INMEMORY]
                        + "; profile single-server-multiproc runs several processes (DESIGN §8)",
                    )
                )
            else:
                items.append(
                    Item(section, name, "backend", "ok", backend, None, _SUMMARY[_INMEMORY])
                )
        elif backend in _NATS:
            items.append(Item(section, name, "backend", "ok", backend, None, _SUMMARY[_NATS]))
        elif backend == _REDIS:
            items.append(Item(section, name, "backend", "ok", backend, None, _SUMMARY[_REDIS]))
        elif backend == _REDIS_PUBSUB:
            items.append(
                Item(
                    section,
                    name,
                    "backend",
                    "unknown",
                    backend,
                    None,
                    "channels_redis pub/sub: semantics not measured (DESIGN §8 covers "
                    "RedisChannelLayer)",
                )
            )
        elif backend in _LITE:
            items.append(Item(section, name, "backend", "ok", backend, None, _SUMMARY[_LITE]))
            items.extend(_lite_items(name, layer, sqlite_files, replicated))
        else:
            items.append(
                Item(section, name, "backend", "unknown", backend, None, "unknown channel layer")
            )
    return items


def _lite_items(
    name: str,
    layer: Mapping[str, Any],
    sqlite_files: Mapping[str, str],
    replicated: set[str] | None,
) -> list[Item]:
    section = "channels"
    config = layer.get("CONFIG")
    alias = config.get("database") if isinstance(config, Mapping) else None
    if not isinstance(alias, str):
        return [
            Item(section, name, "database", "unknown", None, None, "CONFIG['database'] is not set")
        ]
    if alias not in sqlite_files:
        return [
            Item(
                section,
                name,
                "database",
                "unknown",
                alias,
                None,
                "not a SQLite file database alias in DATABASES",
            )
        ]
    real = _real(sqlite_files[alias])
    shared = sorted(
        other for other, path in sqlite_files.items() if other != alias and _real(path) == real
    )
    items = []
    if alias == "default" or shared:
        if alias == "default":
            what = "is the app database (default)"
        else:
            what = "shares a file with " + ", ".join(shared)
        items.append(
            Item(
                section,
                name,
                "database",
                "warn",
                alias,
                None,
                f"channel database {what}; use a separate file (DESIGN §8)",
            )
        )
    else:
        items.append(Item(section, name, "database", "ok", alias, None, "separate file"))
    if replicated is not None:
        if real in replicated:
            items.append(
                Item(
                    section,
                    name,
                    "replicated",
                    "warn",
                    alias,
                    None,
                    "channel database is in the litestream config; exclude it from "
                    "replication (DESIGN §8)",
                )
            )
        else:
            items.append(Item(section, name, "replicated", "ok", alias, None, "not replicated"))
    return items


# --- 조립 ------------------------------------------------------------------------------


def diagnose(
    aliases: Iterable[str] | None = None,
    *,
    litestream_config: str | None = None,
    litestream_binary: str = "litestream",
) -> list[Item]:
    """진단 항목을 모은다. ``aliases`` 가 없으면 sqlite3 엔진 별칭 전부를 본다."""
    items: list[Item] = []
    profile, error = _profile()
    if error is not None:
        items.append(Item("settings", None, "profile", "error", None, None, error.msg))
        profile = DEFAULT_PROFILE
    else:
        items.append(Item("settings", None, "profile", "ok", profile))
    rec = recommended(profile)

    databases = settings.DATABASES
    sqlite = {
        alias: config
        for alias, config in databases.items()
        if isinstance(config, Mapping) and config.get("ENGINE") == ENGINE
    }
    if aliases is None:
        selected = list(sqlite)
    else:
        selected = []
        for alias in dict.fromkeys(aliases):
            if alias not in databases:
                items.append(
                    Item("database", alias, "alias", "error", None, None, "not in DATABASES")
                )
            elif alias not in sqlite:
                items.append(
                    Item(
                        "database",
                        alias,
                        "alias",
                        "unknown",
                        databases[alias].get("ENGINE"),
                        ENGINE,
                        "not a sqlite3 database; not diagnosed",
                    )
                )
            else:
                selected.append(alias)

    # 파일 DB 별칭(쓰기·읽기 전용)의 경로. 메모리·VFS·판정 불가는 빼고 본다.
    files: dict[str, str] = {}
    roles: dict[str, str] = {}
    for alias, config in sqlite.items():
        role, _ = _alias_role(config.get("NAME"))
        roles[alias] = role
        path = _db_file(config.get("NAME"))
        if role in ("write", "read-only") and path is not None:
            files[alias] = path

    for alias in selected:
        items.extend(_diagnose_alias(alias, sqlite[alias], rec))
    for alias in selected:
        if alias in files:
            items.append(_diagnose_mount(alias, files[alias]))

    replicated: set[str] | None = None
    if litestream_config is None:
        items.append(
            Item(
                "litestream",
                None,
                "config",
                "ok",
                None,
                None,
                "skipped: --litestream-config not given",
            )
        )
    else:
        write_aliases = [a for a in selected if roles.get(a) == "write" and a in files]
        ls_items, replicated = _litestream_items(
            litestream_config, litestream_binary, files, write_aliases
        )
        items.extend(ls_items)

    items.extend(_channel_items(files, replicated, profile))
    return items


def exit_code(items: Iterable[Item]) -> int:
    """0 문제 없음 · 1 경고(판정 불가 포함) · 2 오류."""
    levels = {item.level for item in items}
    if "error" in levels:
        return 2
    if levels & {"warn", "unknown"}:
        return 1
    return 0


def _counts(items: list[Item]) -> dict[str, int]:
    return {level: sum(1 for item in items if item.level == level) for level in LEVELS}


def to_json(items: list[Item]) -> str:
    """``--json`` 출력(스키마 ``version: 1``, DESIGN §6-2)."""
    data = {
        "version": SCHEMA_VERSION,
        "items": [asdict(item) for item in items],
        "summary": _counts(items),
        "exit_code": exit_code(items),
    }
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _show(value: Any) -> str:
    return "-" if value is None else str(value)


def format_text(items: list[Item]) -> str:
    """사람용 출력. 섹션별 표, 줄 앞에 수준, 마지막 줄에 요약과 종료 코드."""
    lines = []
    for section in SECTIONS:
        rows = [item for item in items if item.section == section]
        if not rows:
            continue
        lines.append(f"[{section}]")
        alias_w = max(len(_show(r.alias)) for r in rows)
        key_w = max(len(r.key) for r in rows)
        for r in rows:
            value = _show(r.value)
            if r.expected is not None:
                value += f" (expected {r.expected})"
            text = f"  {r.level.upper():<7} {_show(r.alias):<{alias_w}}  {r.key:<{key_w}}  {value}"
            if r.message:
                text += f"  — {r.message}"
            lines.append(text.rstrip())
        lines.append("")
    counts = _counts(items)
    lines.append(
        f"summary: {counts['warn']} warning(s), {counts['error']} error(s), "
        f"{counts['unknown']} unknown -> exit {exit_code(items)}"
    )
    return "\n".join(lines)
