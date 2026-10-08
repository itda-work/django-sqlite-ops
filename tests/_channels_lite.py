"""channels-lite 를 쓰는 테스트의 공통 규칙.

channels-lite[aio] 가 없으면 skip 하지만 ``REQUIRE_CHANNELS_LITE=1`` 이면 실패한다
(CI 의 litestream 잡).
fixture ``channels_lite_aio`` 는 ``conftest.py`` 에 있다.
"""

import importlib.util
import os

import pytest

REQUIRE_CHANNELS_LITE = os.environ.get("REQUIRE_CHANNELS_LITE") == "1"
AIO_MODULES = ("channels_lite", "aiosqlite", "aiosqlitepool")


def skip_or_fail(reason: str) -> None:
    if REQUIRE_CHANNELS_LITE:
        pytest.fail(f"{reason} (REQUIRE_CHANNELS_LITE=1)")
    pytest.skip(reason)


def require_channels_lite_aio() -> None:
    if any(importlib.util.find_spec(m) is None for m in AIO_MODULES):
        skip_or_fail("channels-lite[aio] is not installed")


needs_channels_lite_aio = pytest.mark.usefixtures("channels_lite_aio")
