"""channels-lite aio 레이어의 중복 배달 호환 패치 (DESIGN §9).

``AIOSQLiteChannelLayer._receive_single_from_db`` 는 선점 성공을 ``conn.total_changes > 0`` 으로
판정한다. 풀에서 재사용된 연결이 앞서 쓰기를 했으면 경쟁에서 진 수신자도 같은 메시지를 받는다.
이 모듈은 그 메서드 하나를 ``cursor.rowcount == 1`` 로 판정하는 교체본으로 바꾼다.

- **버전 게이트**: 설치된 channels-lite 가 검증한 판(:data:`VERIFIED_VERSION`)이고 원본 메서드의
  소스가 검증한 소스(:data:`VERIFIED_SOURCE_SHA256`)와 같을 때만 바꾼다. 아니면 바꾸지 않고 사유를
  :func:`status` 로 알려 준다(``sqlite_doctor`` 의 채널 섹션).
- **명시 적용**: 자동으로 적용하지 않는다. ``SQLITE_OPS = {"PATCH_CHANNELS_LITE_AIO": True}`` 이면
  앱 ``ready()`` 에서, 아니면 :func:`apply` 를 직접 부를 때만 적용한다. :func:`apply` 는 멱등이다.
- channels-lite 를 import 할 뿐 DB 는 열지 않는다.
"""

import hashlib
import inspect
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata

__all__ = [
    "SETTING",
    "VERIFIED_SOURCE_SHA256",
    "VERIFIED_VERSION",
    "Status",
    "apply",
    "enabled",
    "status",
]

SETTING = "PATCH_CHANNELS_LITE_AIO"
VERIFIED_VERSION = "0.4.0"
# channels-lite 0.4.0 의 ``inspect.getsource(AIOSQLiteChannelLayer._receive_single_from_db)``.
VERIFIED_SOURCE_SHA256 = "bc15cbf6a5a737eb4b9f4877440857d603dd1fbdb6ec32487aee68374fc6170d"

_METHOD = "_receive_single_from_db"
_MARK = "__sqlite_ops_patch__"
_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class Status:
    """패치 상태. ``applicable`` 은 게이트 통과 여부, ``applied`` 는 이 프로세스에서 적용 여부다."""

    installed: bool
    version: str | None
    applicable: bool
    applied: bool
    reason: str


def _version() -> str | None:
    try:
        return metadata.version("channels-lite")
    except metadata.PackageNotFoundError:
        return None


def _layer_class():
    """``(클래스, None)`` 또는 ``(None, 사유)``."""
    try:
        from channels_lite.layers.aio import AIOSQLiteChannelLayer
    except ImportError as exc:
        return None, f"aio layer is not importable ({exc}); install channels-lite[aio]"
    return AIOSQLiteChannelLayer, None


def _gate(cls) -> str | None:
    """게이트를 통과하면 ``None``, 아니면 사유. 이미 바꾼 클래스는 원본 소스로 판정한다."""
    method = getattr(cls, _METHOD)
    original = getattr(method, "__wrapped_original__", method)
    try:
        source = inspect.getsource(original)
    except (OSError, TypeError) as exc:
        return f"source of {_METHOD} is not available ({exc}); cannot verify"
    if hashlib.sha256(source.encode()).hexdigest() != VERIFIED_SOURCE_SHA256:
        return f"{_METHOD} differs from the verified channels-lite {VERIFIED_VERSION} source"
    return None


def status() -> Status:
    """현재 상태를 돌려준다. 아무것도 바꾸지 않는다."""
    version = _version()
    if version is None:
        return Status(False, None, False, False, "channels-lite is not installed")
    if version != VERIFIED_VERSION:
        return Status(
            True,
            version,
            False,
            False,
            f"channels-lite {version} is not verified (verified: =={VERIFIED_VERSION}); "
            "patch not applied",
        )
    cls, reason = _layer_class()
    if cls is None:
        return Status(True, version, False, False, reason)
    reason = _gate(cls)
    if reason is not None:
        return Status(True, version, False, False, reason + "; patch not applied")
    if getattr(getattr(cls, _METHOD), _MARK, False):
        return Status(True, version, True, True, "applied")
    return Status(True, version, True, False, "not applied in this process")


def apply() -> bool:
    """게이트를 통과하면 패치를 적용하고 ``True``, 아니면 아무것도 바꾸지 않고 ``False``.

    두 번 불러도 한 번만 바꾼다. channels-lite 가 없으면 조용히 ``False`` 다.
    """
    with _lock:
        current = status()
        if current.applied:
            return True
        if not current.applicable:
            return False
        from channels_lite.layers.aio import AIOSQLiteChannelLayer

        from ._channels_lite_aio import _receive_single_from_db as patched

        # 원본은 게이트 판정(_gate)이 다시 볼 수 있도록 교체본에 붙여 둔다
        patched.__wrapped_original__ = getattr(AIOSQLiteChannelLayer, _METHOD)
        patched.__qualname__ = f"AIOSQLiteChannelLayer.{_METHOD}"
        setattr(patched, _MARK, True)
        setattr(AIOSQLiteChannelLayer, _METHOD, patched)
        return True


def enabled(config: object) -> bool:
    """``SQLITE_OPS`` 에서 패치가 켜져 있는지. 정확히 ``True`` 일 때만 켠다."""
    return isinstance(config, Mapping) and config.get(SETTING) is True
