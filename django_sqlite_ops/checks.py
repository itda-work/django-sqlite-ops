"""정적 시스템 체크 (DESIGN §6-1). 설정만 읽고 DB 를 열지 않는다.

기준값은 ``database.recommended()`` 에서 읽는다. ``sqlite_database()`` 를 쓰지 않은
설정(직접 쓴 dict, dj-lite 결과)도 같은 기준으로 검사한다.
"""

import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

from django.conf import settings
from django.core import checks

from .database import DEFAULT_PROFILE, ENGINE, PROFILES, recommended

__all__ = ["TAG", "check_deploy_settings", "check_settings", "register"]

# Tags.database 를 쓰지 않는다. Django 6.1 은 --database 없이 돌면 database 태그 체크를 빼고
# (6.1.2 django/core/checks/registry.py:89-93), 5.2 는 빼지 않는다(5.2.18 같은 파일의
# run_checks :72-96 에 그 분기가 없다). 두 지원 버전에서 기본으로 돌도록 자체 태그를 쓴다.
TAG = "sqlite_ops"

# 식별자: "..." · `...` · [...] 인용, 또는 맨 이름. 값: 인용 문자열, 이름, 정수.
_IDENT = r"""(?:"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_$]*)"""
_VALUE = r"""(?:'(?:[^']|'')*'|"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*|[-+]?\d+)"""
_PRAGMA = re.compile(
    rf"""\s*PRAGMA\s+(?:(?P<schema>{_IDENT})\s*\.\s*)?(?P<name>{_IDENT})\s*
        (?:=\s*(?P<eq>{_VALUE})|\(\s*(?P<call>{_VALUE})\s*\))?\s*""",
    re.IGNORECASE | re.VERBOSE,
)
# SQLite 가 받는 journal_mode 값. 그 밖의 값은 SQLite 가 무시하므로 결과를 단정할 수 없다.
_JOURNAL_MODES = frozenset({"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"})
_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}


def register() -> None:
    checks.register(check_settings, TAG)
    checks.register(check_deploy_settings, TAG, deploy=True)


def _profile() -> tuple[str | None, checks.Error | None]:
    config = getattr(settings, "SQLITE_OPS", {})
    if not isinstance(config, Mapping):
        return None, checks.Error(
            f"SQLITE_OPS must be a dict, got {type(config).__name__}.",
            hint=f'Use SQLITE_OPS = {{"PROFILE": "{DEFAULT_PROFILE}"}} (DESIGN §6-0).',
            id="sqlite_ops.E001",
        )
    profile = config.get("PROFILE", DEFAULT_PROFILE)
    if not isinstance(profile, str) or profile not in PROFILES:
        return None, checks.Error(
            f"Unknown SQLITE_OPS['PROFILE'] {profile!r}.",
            hint=f"Use one of: {', '.join(PROFILES)} (DESIGN §6-0).",
            id="sqlite_ops.E001",
        )
    return profile, None


def _sqlite_aliases():
    for alias, config in settings.DATABASES.items():
        if isinstance(config, Mapping) and config.get("ENGINE") == ENGINE:
            yield alias, config


def _options(config: Mapping[str, Any]) -> Mapping[str, Any]:
    options = config.get("OPTIONS") or {}
    return options if isinstance(options, Mapping) else {}


def _uri(name: Any) -> tuple[str, dict[str, list[str]]] | None:
    """``file:`` URI 면 (파일명, 쿼리)를, 아니면 ``None`` 을 돌려준다.

    SQLite 처럼 파일명은 첫 ``?``·``#`` 앞, 쿼리는 ``?`` 뒤 ``#`` 앞이다. 구분자로 먼저 나눈 뒤
    파일명과 쿼리 키·값을 퍼센트 디코딩한다(인코딩된 ``%3F`` 를 구분자로 보지 않는다).
    """
    if isinstance(name, os.PathLike):
        name = os.fspath(name)
    if not isinstance(name, str) or not name.startswith("file:"):
        return None
    rest = name[len("file:") :]
    if rest.startswith("//"):
        # authority 는 "//" 뒤 다음 "/" 까지다(쿼리·프래그먼트 구분자보다 먼저 본다). 로컬
        # authority 는 떼어 낸다. 그 밖의 authority 는 _uri_authority() 가 판정한다.
        end = rest.find("/", 2)
        rest = rest[end:] if end >= 0 else ""
    rest = rest.split("#", 1)[0]
    path, _, raw_query = rest.partition("?")
    query: dict[str, list[str]] = {}
    for part in raw_query.split("&"):
        if part:
            key, _, value = part.partition("=")
            query.setdefault(unquote(key), []).append(unquote(value))
    return unquote(path), query


def _uri_authority(name: Any) -> str | None:
    """``file://`` URI 의 authority 가 SQLite 가 받지 않는 값이면 그 값을, 아니면 ``None``.

    SQLite 는 authority 로 빈 문자열과 ``localhost`` 만 받는다(https://www.sqlite.org/uri.html).
    authority 는 ``//`` 뒤 다음 ``/`` 까지이고, 대소문자를 가리며 퍼센트 디코딩하지 않는다
    (``LOCALHOST``·``%6cocalhost``·``localhost?mode=ro`` 는 연결 오류, 재현함). Windows 의
    ``SQLITE_ALLOW_URI_AUTHORITY`` UNC 경로는 흉내 내지 않는다.
    """
    if isinstance(name, os.PathLike):
        name = os.fspath(name)
    if not isinstance(name, str) or not name.startswith("file://"):
        return None
    rest = name[len("file://") :]
    end = rest.find("/")
    authority = rest[:end] if end >= 0 else rest
    return None if authority in ("", "localhost") else authority


# SQLite 가 받는 값 (https://www.sqlite.org/uri.html, sqlite3_uri_boolean). 그 밖은 판정하지 않는다.
_MODES = frozenset({"ro", "rw", "rwc", "memory"})
_TRUE = frozenset({"1", "yes", "true", "on"})
_FALSE = frozenset({"0", "no", "false", "off"})
_ROLE_KEYS = ("mode", "immutable", "vfs")


def _role(name: Any) -> tuple[str, str | None]:
    """별칭의 역할과, 판정할 수 없을 때 그 이유를 돌려준다.

    역할: ``"write"`` · ``"memory"`` · ``"read-only"`` · ``"vfs"`` · ``"unknown"``.
    역할을 정하는 쿼리 키(``mode``·``immutable``·``vfs``)가 겹치거나 SQLite 표준 표기가 아니면,
    또는 URI authority 가 빈 값·``localhost`` 가 아니면 ``"unknown"`` 이다. SQLite 의
    중복·비표준 해석을 흉내 내지 않는다.
    """
    if isinstance(name, os.PathLike):
        name = os.fspath(name)
    if name == ":memory:":
        return "memory", None
    uri = _uri(name)
    if uri is None:
        return "write", None
    authority = _uri_authority(name)
    if authority is not None:
        return "unknown", f"URI authority {authority!r} is not empty or 'localhost'"
    path, query = uri
    # SQLite 는 디코딩된 NUL 앞까지만 읽는다. 역할 키가 숨을 수 있으니 어느 키든 판정하지 않는다
    if "\0" in path:
        return "unknown", "the decoded file name contains NUL (%00)"
    for key, values in query.items():
        if "\0" in key or any("\0" in value for value in values):
            return "unknown", f"query key {key!r} or its value contains NUL (%00)"
    for key in _ROLE_KEYS:
        if len(query.get(key, [])) > 1:
            return "unknown", f"query key {key!r} is given more than once"
    mode = query.get("mode", [None])[0]
    if mode is not None and mode not in _MODES:
        return "unknown", f"mode={mode!r} is not one of {', '.join(sorted(_MODES))}"
    immutable = query.get("immutable", [None])[0]
    if immutable is not None and immutable.lower() not in _TRUE | _FALSE:
        return (
            "unknown",
            f"immutable={immutable!r} is not a boolean (1/0, yes/no, true/false, on/off)",
        )
    if path == ":memory:" or mode == "memory":
        return "memory", None
    if mode == "ro" or (immutable is not None and immutable.lower() in _TRUE):
        return "read-only", None
    if query.get("vfs") == ["litestream"]:
        return "vfs", None
    return "write", None


def _is_litestream_vfs(name: Any) -> bool:
    uri = _uri(name)
    return uri is not None and uri[1].get("vfs") == ["litestream"]


def _strip_comments(statement: str) -> str:
    """SQL 주석(``--``, ``/* */``)을 공백으로 바꾼다. 인용 문자열·식별자 안은 그대로 둔다."""
    out = []
    i, n = 0, len(statement)
    while i < n:
        ch = statement[i]
        if ch in _QUOTES:
            close = _QUOTES[ch]
            j = i + 1
            while j < n:
                if statement[j] == close:
                    if close != "]" and statement[j + 1 : j + 2] == close:
                        j += 2  # 겹친 인용 부호
                        continue
                    break
                j += 1
            out.append(statement[i : j + 1])
            i = j + 1
        elif statement.startswith("--", i):
            j = statement.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif statement.startswith("/*", i):
            j = statement.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _unquote(token: str) -> str:
    if token and token[0] in _QUOTES:
        close = _QUOTES[token[0]]
        inner = token[1:-1]
        return inner if close == "]" else inner.replace(close * 2, close)
    return token


def _journal_mode(init_command: Any) -> tuple[str | None, str | None]:
    """``init_command`` 에서 main 의 ``journal_mode`` 를 읽는다.

    ``(마지막으로 확정된 값(대문자) 또는 None, 형식을 확정할 수 없는 첫 문장 또는 None)``.
    Django 처럼 ``;`` 로 나눈 문장마다 본다. SQL 파서가 아니므로 ``journal_mode`` 를 언급하는데
    아는 형식이 아니면 판정할 수 없다고 본다.
    """
    if not isinstance(init_command, str):
        return None, None
    mode = undetermined = None
    for statement in init_command.split(";"):
        cleaned = _strip_comments(statement)
        if "journal_mode" not in cleaned.lower():
            continue
        match = _PRAGMA.fullmatch(cleaned)
        if match is None:
            undetermined = undetermined or statement.strip()
            continue
        if _unquote(match["name"]).lower() != "journal_mode":
            continue
        if match["schema"] and _unquote(match["schema"]).lower() != "main":
            continue  # 다른 스키마(temp 등)는 이 DB 의 모드를 바꾸지 않는다
        value = match["eq"] or match["call"]
        if value is None:
            continue  # 조회만 한다
        value = _unquote(value).upper()
        if value not in _JOURNAL_MODES:
            undetermined = undetermined or statement.strip()
            continue
        mode = value
    return mode, undetermined


def _health_errors() -> list[checks.Error]:
    """E002: ``SQLITE_OPS["HEALTH"]`` 가 잘못됨. ``HEALTH`` 가 없으면 검사하지 않는다(DESIGN §7)."""
    from .health import load_config  # health 는 doctor → checks 를 import 한다

    _, errors = load_config()
    return [
        checks.Error(
            message,
            hint="See README '복제 헬스' and DESIGN §7 for the HEALTH settings.",
            obj=alias,
            id="sqlite_ops.E002",
        )
        for message, alias in errors
    ]


def check_settings(app_configs=None, **kwargs):
    """항상 도는 체크: E001(프로필), E002(헬스 설정), W003(VFS 별칭의 ``CONN_MAX_AGE``)."""
    _, error = _profile()
    if error:
        return [error]
    messages: list = _health_errors()
    if getattr(settings, "ASGI_APPLICATION", None):
        # ASGI 의 영속 연결은 미검증이다. 보편적으로 강제하지 않는다 (DESIGN §6-1).
        return messages
    for alias, config in _sqlite_aliases():
        if _is_litestream_vfs(config.get("NAME")) and config.get("CONN_MAX_AGE", 0) is not None:
            messages.append(
                checks.Warning(
                    f"Litestream VFS database {alias!r} reconnects per request "
                    "(CONN_MAX_AGE is not None).",
                    hint=(
                        f'Set DATABASES["{alias}"]["CONN_MAX_AGE"] = None; measured under WSGI: '
                        "1,008ms per request with 0, 1.7ms with None (DESIGN §6-0)."
                    ),
                    obj=alias,
                    id="sqlite_ops.W003",
                )
            )
    return messages


def check_deploy_settings(app_configs=None, **kwargs):
    """``check --deploy`` 에서만 도는 체크: W001(transaction_mode), W002(journal_mode),
    W004(별칭 역할을 판정할 수 없음)."""
    profile, error = _profile()
    if error:
        return []  # E001 은 check_settings 가 낸다
    rec = recommended(profile)
    want_mode = rec["transaction_mode"].upper()
    want_journal = str(rec["pragmas"]["journal_mode"]).upper()
    messages = []
    for alias, config in _sqlite_aliases():
        role, reason = _role(config.get("NAME"))
        if role == "unknown":
            # 역할을 모르면 쓰기 권고를 내지 않는다. 따르면 연결이 깨질 수 있다(mode=ro 에 WAL)
            messages.append(
                checks.Warning(
                    f"Cannot determine the role of database {alias!r} (write, read-only, "
                    f"memory or Litestream VFS) from its NAME: {reason}.",
                    hint=(
                        "Rewrite NAME as a standard URI: no authority other than "
                        "'localhost' (file:///path or file:/path), no %00, each query key "
                        "(mode, immutable, vfs) once, mode=ro|rw|rwc|memory, immutable=1|0, "
                        "vfs=<name> (DESIGN §6-1)."
                    ),
                    obj=alias,
                    id="sqlite_ops.W004",
                )
            )
            continue
        if role in ("read-only", "vfs"):
            # 쓰기 권고가 의미 없거나, 따르면 연결이 깨진다(mode=ro 에 journal_mode=WAL)
            continue
        options = _options(config)
        mode = options.get("transaction_mode")
        if not isinstance(mode, str) or mode.upper() != want_mode:
            messages.append(
                checks.Warning(
                    f"Database {alias!r} has OPTIONS['transaction_mode'] = {mode!r}, "
                    f"not {want_mode!r}.",
                    hint=(
                        "Use sqlite_database() or set "
                        f'OPTIONS["transaction_mode"] = "{want_mode}" (DESIGN §6-0).'
                    ),
                    obj=alias,
                    id="sqlite_ops.W001",
                )
            )
        if role == "memory":
            continue
        journal, undetermined = _journal_mode(options.get("init_command"))
        if undetermined is not None:
            messages.append(
                checks.Warning(
                    f"Database {alias!r}: cannot determine journal_mode from "
                    f"OPTIONS['init_command'] statement {undetermined!r}.",
                    hint=(
                        "Use sqlite_database() or write it as "
                        f'"PRAGMA journal_mode={want_journal}" (DESIGN §6-1).'
                    ),
                    obj=alias,
                    id="sqlite_ops.W002",
                )
            )
        elif journal != want_journal:
            messages.append(
                checks.Warning(
                    f"Database {alias!r} does not set journal_mode={want_journal} in "
                    "OPTIONS['init_command'].",
                    hint=(
                        "Use sqlite_database() or add "
                        f'"PRAGMA journal_mode={want_journal}" to OPTIONS["init_command"] '
                        "(DESIGN §6-0)."
                    ),
                    obj=alias,
                    id="sqlite_ops.W002",
                )
            )
    return messages
