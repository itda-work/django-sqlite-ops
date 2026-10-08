"""정적 시스템 체크 (DESIGN §6-1). 설정만 읽고 DB 를 열지 않는다.

기준값은 ``database.recommended()`` 에서 읽는다. ``sqlite_database()`` 를 쓰지 않은
설정(직접 쓴 dict, dj-lite 결과)도 같은 기준으로 검사한다.
"""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs

from django.conf import settings
from django.core import checks

from .database import DEFAULT_PROFILE, ENGINE, PROFILES, recommended

__all__ = ["TAG", "check_deploy_settings", "check_settings", "register"]

# Tags.database 는 쓰지 않는다. 그 태그는 check --database 없이는 돌지 않는다(DB 를 여는 체크용).
TAG = "sqlite_ops"

# Django 처럼 init_command 를 ";" 로 나눈 각 문장에서 찾는다. 스키마는 main 만 이 DB 에 적용된다.
_JOURNAL_MODE = re.compile(
    r"""\s*PRAGMA\s+(?:(?:main|"main"|`main`|\[main\])\s*\.\s*)?journal_mode\s*
        (?:=\s*(?P<eq>'[^']*'|"[^"]*"|\w+)|\(\s*(?P<call>'[^']*'|"[^"]*"|\w+)\s*\))\s*""",
    re.IGNORECASE | re.VERBOSE,
)


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


def _uri_query(name: str) -> dict[str, list[str]]:
    if not name.startswith("file:") or "?" not in name:
        return {}
    return parse_qs(name.split("?", 1)[1].split("#", 1)[0])


def _is_memory_db(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    if name == ":memory:" or name.startswith("file::memory:"):
        return True
    return "memory" in _uri_query(name).get("mode", [])


def _is_litestream_vfs(name: Any) -> bool:
    return isinstance(name, str) and "litestream" in _uri_query(name).get("vfs", [])


def _journal_mode(init_command: Any) -> str | None:
    """``init_command`` 에서 마지막으로 설정된 ``journal_mode`` 값(대문자)을 돌려준다."""
    if not isinstance(init_command, str):
        return None
    mode = None
    for statement in init_command.split(";"):
        if match := _JOURNAL_MODE.fullmatch(statement):
            mode = (match["eq"] or match["call"]).strip("'\"").upper()
    return mode


def check_settings(app_configs=None, **kwargs):
    """항상 도는 체크: E001(프로필), W003(VFS 별칭의 ``CONN_MAX_AGE``)."""
    _, error = _profile()
    if error:
        return [error]
    if getattr(settings, "ASGI_APPLICATION", None):
        # ASGI 의 영속 연결은 미검증이다. 보편적으로 강제하지 않는다 (DESIGN §6-1).
        return []
    messages = []
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
    """``check --deploy`` 에서만 도는 체크: W001(transaction_mode), W002(journal_mode)."""
    profile, error = _profile()
    if error:
        return []  # E001 은 check_settings 가 낸다
    rec = recommended(profile)
    want_mode = rec["transaction_mode"].upper()
    want_journal = str(rec["pragmas"]["journal_mode"]).upper()
    messages = []
    for alias, config in _sqlite_aliases():
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
        if _is_memory_db(config.get("NAME")):
            continue
        journal = _journal_mode(options.get("init_command"))
        if journal != want_journal:
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
