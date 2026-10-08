import pytest
from _litestream import require_litestream


@pytest.fixture
def litestream_binary() -> str:
    return require_litestream()
