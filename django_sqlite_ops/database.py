"""권장 SQLite 설정 표와 표준 ``DATABASES`` 항목 생성 함수 (DESIGN §6-0).

``settings.py`` 에서 부르는 모듈이라 표준 라이브러리만 import 한다. Django 를 import 하지 않는다.
"""

import re
from collections.abc import Mapping
from os import PathLike
from types import MappingProxyType
from typing import Any

__all__ = ["DEFAULT_PROFILE", "PROFILES", "RECOMMENDED", "recommended", "sqlite_database"]

ENGINE = "django.db.backends.sqlite3"
DEFAULT_PROFILE = "single-server"

# 권장값의 정본. 실측 근거가 있는 값만 넣는다(DESIGN §6-0 표의 "기본 적용 = 켬").
# foreign_keys 는 Django 가 이미 켜고, wal_autocheckpoint 는 Litestream 이 관리하므로 넣지 않는다.
_SINGLE_SERVER = MappingProxyType(
    {
        "transaction_mode": "IMMEDIATE",
        "pragmas": MappingProxyType(
            {
                "journal_mode": "WAL",
                "synchronous": "NORMAL",
                "busy_timeout": 5000,
            }
        ),
    }
)

RECOMMENDED: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "single-server": _SINGLE_SERVER,
        # v0.1 에서는 DB 설정이 같다. 프로필별로 달라질 수 있게 따로 둔다.
        "single-server-multiproc": _SINGLE_SERVER,
    }
)
PROFILES = tuple(RECOMMENDED)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Django 는 init_command 를 ";" 로 나눠 실행한다. 값에 ";" 나 공백·따옴표가 들어오지 못하게 한다.
_PRAGMA_VALUE = re.compile(r"-?[A-Za-z0-9_]+")


def recommended(profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
    """프로필의 권장값을 복사해서 돌려준다: ``{"transaction_mode": ..., "pragmas": {...}}``."""
    try:
        entry = RECOMMENDED[profile]
    except KeyError:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {', '.join(PROFILES)}"
        ) from None
    return {"transaction_mode": entry["transaction_mode"], "pragmas": dict(entry["pragmas"])}


def _check_pragma_name(name: Any) -> None:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"invalid PRAGMA name {name!r}; must be an SQL identifier")


def _format_pragma(name: str, value: Any) -> str:
    if isinstance(value, bool):
        text = "ON" if value else "OFF"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str) and _PRAGMA_VALUE.fullmatch(value):
        text = value
    else:
        raise ValueError(
            f"invalid value {value!r} for PRAGMA {name}; use an int, bool or bare keyword"
        )
    return f"PRAGMA {name}={text}"


def sqlite_database(
    name: str | PathLike[str],
    *,
    profile: str = DEFAULT_PROFILE,
    pragmas: Mapping[str, Any] | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """권장값을 담은 ``DATABASES`` 항목을 돌려준다.

    ``pragmas`` 는 PRAGMA 를 덮거나 더한다. 값이 ``None`` 이면 그 PRAGMA 를 뺀다.
    PRAGMA 이름은 SQLite 처럼 대소문자를 가리지 않고 소문자로 맞춘다.
    ``options`` 는 ``OPTIONS`` 키를 더하거나 덮는다. ``init_command`` 는 ``pragmas`` 로만 정한다.

    ``options={"timeout": N}``(초)만 주면 대기 시간이 바뀌지 않는다. ``init_command`` 의
    ``busy_timeout``(밀리초, 기본 5000)이 연결 뒤에 실행되어 우선하기 때문이다. ``timeout`` 을
    쓰려면 ``pragmas={"busy_timeout": None}`` 을 함께 주고, 아니면 ``busy_timeout`` 을 덮는다.
    """
    rec = recommended(profile)
    if options and "init_command" in options:
        raise ValueError(
            "options['init_command'] conflicts with the generated PRAGMAs; "
            "use pragmas={...} instead"
        )

    merged = rec["pragmas"]
    seen: set[str] = set()
    for key, value in (pragmas or {}).items():
        _check_pragma_name(key)
        key = key.lower()
        if key in seen:
            raise ValueError(f"PRAGMA {key} given more than once in pragmas (names ignore case)")
        seen.add(key)
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    statements = [_format_pragma(key, value) for key, value in merged.items()]

    result_options: dict[str, Any] = {"transaction_mode": rec["transaction_mode"]}
    if statements:
        result_options["init_command"] = ";".join(statements)
    result_options.update(options or {})
    return {"ENGINE": ENGINE, "NAME": str(name), "OPTIONS": result_options}
