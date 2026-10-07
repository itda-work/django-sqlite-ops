"""부팅 판정 순수 함수 (DESIGN §4-3, §4-4).

입력만 보고 상태와 조치를 정한다. 파일·subprocess·시간·환경변수를 건드리지 않는다.
Django 를 import 하지 않는다(DESIGN §4-1).

공개 입력은 정확한 타입(``type(x) is ...``)만 받는다. 판정이 로컬 DB 를 지킬지 덮을지를
정하므로 사용자 객체의 연산(``__eq__``·``__hash__``·``__bool__``·``__lt__``·``__repr__`` …)을
신뢰하지 않는다.
검증은 ``_normalize()`` 한 곳에서 하고, 그 뒤의 분류·조치는 정규화된 내장 값만 본다.
"""

from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from typing import Any

__all__ = [
    "Action",
    "Decision",
    "OnUnknown",
    "ReasonCode",
    "Remote",
    "RemoteEmpty",
    "RemoteError",
    "RemoteTxid",
    "State",
    "decide",
]


class State(StrEnum):
    FRESH = "fresh"
    MATCH = "match"
    ADOPT = "adopt"
    UNKNOWN = "unknown"


class Action(StrEnum):
    PROCEED = "proceed"
    RESTORE = "restore"
    QUARANTINE_AND_RESTORE = "quarantine_and_restore"
    KEEP_LOCAL = "keep_local"
    REFUSE = "refuse"


class ReasonCode(StrEnum):
    # fresh
    NEW_DB = "new_db"
    RESTORE_FROM_REMOTE = "restore_from_remote"
    # match
    LOCAL_CURRENT = "local_current"
    # adopt (D-11)
    ADOPT_EXISTING = "adopt_existing"
    # unknown
    REMOTE_ERROR = "remote_error"
    STALE_META = "stale_meta"
    NO_LOCAL_META = "no_local_meta"
    REMOTE_EMPTY = "remote_empty"
    REMOTE_AHEAD = "remote_ahead"
    # 로컬 DB·메타 없음 + 원격 빈 목록, init_new 없음(D-13). 복제본 경로 오타와 구분되지 않는다.
    NO_REPLICA_NO_LOCAL = "no_replica_no_local"


class OnUnknown(StrEnum):
    REFUSE = "refuse"
    RESTORE = "restore"
    KEEP_LOCAL = "keep-local"


@dataclass(frozen=True, slots=True)
class RemoteError:
    """원격 조회 실패(타임아웃·인증·파싱 실패 모두)."""

    message: str

    def __post_init__(self) -> None:
        _check_message(self.message)


@dataclass(frozen=True, slots=True)
class RemoteEmpty:
    """원격 조회 성공, 복제본 없음."""


@dataclass(frozen=True, slots=True)
class RemoteTxid:
    """원격 조회 성공, 복제본의 최대 TXID."""

    txid: int

    def __post_init__(self) -> None:
        _check_txid("remote txid", self.txid)


Remote = RemoteError | RemoteEmpty | RemoteTxid


@dataclass(frozen=True, slots=True)
class Decision:
    state: State
    action: Action
    reason_code: ReasonCode
    reason: str


_SAFE_REPR = (type(None), bool, int, str)


def _show(value: Any) -> str:
    # 거부할 값의 __repr__ 도 사용자 코드다. 내장 값만 repr 하고 나머지는 타입 이름만 쓴다.
    kind = type(value)
    if any(kind is safe for safe in _SAFE_REPR):
        return repr(value)
    return f"<{kind.__qualname__} object>"


def _check_txid(label: str, value: Any) -> int:
    # 내장 int 만 받는다. 서브클래스는 비교 연산을 바꿔 검증과 판정을 우회할 수 있다.
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {label} {_show(value)}; must be a non-negative int")
    return value


def _check_message(value: Any) -> str:
    if type(value) is not str:
        raise ValueError(f"invalid remote error message {_show(value)}; must be a str")
    return value


def _check_bool(label: str, value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError(f"invalid {label} {_show(value)}; must be a bool")
    return value


_POLICIES = {p.value: p for p in OnUnknown}


def _check_policy(value: Any) -> OnUnknown:
    # OnUnknown(value) 는 값의 __hash__·__eq__ 로 찾으므로 쓰지 않는다.
    if type(value) is OnUnknown:
        return value
    if type(value) is str and value in _POLICIES:
        return _POLICIES[value]
    choices = ", ".join(_POLICIES)
    raise ValueError(f"invalid on_unknown {_show(value)}; expected one of {choices}")


class _Kind(Enum):
    ERROR = auto()
    EMPTY = auto()
    TXID = auto()


@dataclass(frozen=True, slots=True)
class _Remote:
    """API 경계에서 한 번 정규화한 원격 결과. 분류와 조치는 이 태그만 본다."""

    kind: _Kind
    txid: int | None = None
    message: str = ""

    @property
    def text(self) -> str:
        if self.kind is _Kind.TXID:
            return f"txid {self.txid}"
        if self.kind is _Kind.EMPTY:
            return "empty"
        return f"error: {self.message}"


def _normalize_remote(remote: Any) -> _Remote:
    # isinstance 는 __class__ 를 속인 객체에 둘 이상 참이 될 수 있어 정확한 타입으로 가른다.
    # 필드는 frozen 이라도 object.__setattr__ 로 바뀔 수 있으므로 생성자 검증과 별도로 다시 본다.
    kind = type(remote)
    if kind is RemoteError:
        message = _check_message(remote.message)
        # reason 은 한 줄이어야 한다. str.splitlines 의 줄 구분자를 모두 공백으로 바꾼다.
        return _Remote(_Kind.ERROR, message=" ".join(message.splitlines()))
    if kind is RemoteEmpty:
        return _Remote(_Kind.EMPTY)
    if kind is RemoteTxid:
        return _Remote(_Kind.TXID, txid=_check_txid("remote txid", remote.txid))
    raise ValueError(
        f"invalid remote {_show(remote)}; expected exactly RemoteError, RemoteEmpty or RemoteTxid"
    )


@dataclass(frozen=True, slots=True)
class _Inputs:
    """정규화된 입력. 모든 필드가 내장 값이거나 이 모듈의 타입이다."""

    local_exists: bool
    local_txid: int | None
    remote: _Remote
    policy: OnUnknown
    adopt_existing: bool
    init_new: bool


def _normalize(
    local_exists: Any,
    local_txid: Any,
    remote: Any,
    on_unknown: Any,
    adopt_existing: Any,
    init_new: Any,
) -> _Inputs:
    """공개 입력을 검증하는 유일한 지점. 정확한 타입이 아니면 dunder 를 부르기 전에 거부한다."""
    return _Inputs(
        local_exists=_check_bool("local_exists", local_exists),
        local_txid=None if local_txid is None else _check_txid("local txid", local_txid),
        remote=_normalize_remote(remote),
        policy=_check_policy(on_unknown),
        adopt_existing=_check_bool("adopt_existing", adopt_existing),
        init_new=_check_bool("init_new", init_new),
    )


def _classify(inp: _Inputs) -> tuple[State, ReasonCode, str]:
    local_exists, local_txid, remote = inp.local_exists, inp.local_txid, inp.remote
    # 표의 순서대로 본다(DESIGN §4-3).
    # DB 없이 메타만 남은 경우는 원격 결과와 무관하게 stale_meta 다.
    if not local_exists and local_txid is not None:
        return (
            State.UNKNOWN,
            ReasonCode.STALE_META,
            f"no local db but local meta has txid {local_txid}; remote {remote.text}",
        )
    if remote.kind is _Kind.ERROR:
        where = "local db exists" if local_exists else "no local db"
        return (
            State.UNKNOWN,
            ReasonCode.REMOTE_ERROR,
            f"{where}, local txid {local_txid}; remote lookup failed: {remote.message}",
        )
    if not local_exists:
        if remote.kind is _Kind.EMPTY:
            # D-13: 빈 목록은 '복제본 없음'과 경로·prefix 오타를 구분하지 못한다(#3 실측).
            if inp.init_new:
                return State.FRESH, ReasonCode.NEW_DB, "no local db and remote empty; start new db"
            return (
                State.UNKNOWN,
                ReasonCode.NO_REPLICA_NO_LOCAL,
                "no local db and remote empty (replica path may be wrong); "
                "use --init-new for the first deploy",
            )
        return (
            State.FRESH,
            ReasonCode.RESTORE_FROM_REMOTE,
            f"no local db; restore remote txid {remote.txid}",
        )

    if local_txid is None:
        if inp.adopt_existing and remote.kind is _Kind.EMPTY:
            return (
                State.ADOPT,
                ReasonCode.ADOPT_EXISTING,
                "local db exists without meta and remote is empty; adopting existing db",
            )
        return (
            State.UNKNOWN,
            ReasonCode.NO_LOCAL_META,
            f"local db exists but local meta is missing; remote {remote.text}",
        )
    if remote.kind is _Kind.EMPTY:
        return (
            State.UNKNOWN,
            ReasonCode.REMOTE_EMPTY,
            f"local db exists with txid {local_txid} but remote is empty",
        )
    assert remote.txid is not None
    if local_txid >= remote.txid:
        return (
            State.MATCH,
            ReasonCode.LOCAL_CURRENT,
            f"local txid {local_txid} >= remote txid {remote.txid}",
        )
    return (
        State.UNKNOWN,
        ReasonCode.REMOTE_AHEAD,
        f"local txid {local_txid} < remote txid {remote.txid}",
    )


def decide(
    *,
    local_exists: bool,
    local_txid: int | None,
    remote: Remote,
    on_unknown: OnUnknown | str = OnUnknown.REFUSE,
    adopt_existing: bool = False,
    init_new: bool = False,
) -> Decision:
    """부팅 상태(fresh/match/adopt/unknown)와 조치를 정한다. 규칙표는 DESIGN §4-3."""
    inp = _normalize(local_exists, local_txid, remote, on_unknown, adopt_existing, init_new)
    state, code, reason = _classify(inp)

    if state is State.FRESH:
        action = Action.PROCEED if code is ReasonCode.NEW_DB else Action.RESTORE
    elif state is State.MATCH or state is State.ADOPT:
        action = Action.PROCEED
    elif inp.remote.kind is _Kind.ERROR:
        # D-12: 원격 조회 실패면 정책과 무관하게 거부한다(S3 장애 중 옛 볼륨 재부팅 방지).
        action = Action.REFUSE
    elif inp.policy is OnUnknown.RESTORE:
        # 복원할 복제본이 있을 때만 격리 후 복원한다. 빈 목록이면 거부한다.
        action = Action.QUARANTINE_AND_RESTORE if inp.remote.kind is _Kind.TXID else Action.REFUSE
    elif inp.policy is OnUnknown.KEEP_LOCAL:
        # 로컬 DB 가 없으면 지킬 것이 없고, 새 DB 로 시작하면 원격을 덮을 수 있다.
        action = Action.KEEP_LOCAL if inp.local_exists else Action.REFUSE
    else:
        action = Action.REFUSE

    return Decision(state=state, action=action, reason_code=code, reason=reason)
