import pytest
from _channels_lite import require_channels_lite_aio
from _litestream import require_litestream


@pytest.fixture
def litestream_binary() -> str:
    return require_litestream()


@pytest.fixture
def channels_lite_aio() -> None:
    require_channels_lite_aio()
