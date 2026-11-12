import pytest
from _channels_lite import require_channels_lite_aio
from _litestream import (
    FIXTURE_EXPECTED,
    LitestreamFixtures,
    litestream_fixtures,
    require_litestream,
)


@pytest.fixture
def litestream_binary() -> str:
    return require_litestream()


@pytest.fixture
def channels_lite_aio() -> None:
    require_channels_lite_aio()


@pytest.fixture(params=list(FIXTURE_EXPECTED))
def lsfx(request) -> LitestreamFixtures:
    """검증한 Litestream 판마다 한 번씩: 그 판의 실측 fixture 와 기대값."""
    return litestream_fixtures(request.param)
