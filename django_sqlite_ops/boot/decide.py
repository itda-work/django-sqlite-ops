"""부팅 판정 순수 함수 (DESIGN §4-3, §4-4).

입력만 보고 상태와 조치를 정한다. 파일·subprocess·시간·환경변수를 건드리지 않는다.
Django 를 import 하지 않는다(DESIGN §4-1).
"""

from dataclasses import dataclass
from enum import StrEnum
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
    # unknown
    REMOTE_ERROR = "remote_error"
    STALE_META = "stale_meta"
    NO_LOCAL_META = "no_local_meta"
    REMOTE_EMPTY = "remote_empty"
    REMOTE_AHEAD = "remote_ahead"


class OnUnknown(StrEnum):
    REFUSE = "refuse"
    RESTORE = "restore"
    KEEP_LOCAL = "keep-local"


@dataclass(frozen=True, slots=True)
class RemoteError:
    """원격 조회 실패(타임아웃·인증·파싱 실패 모두)."""

    message: str


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


def _check_txid(label: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {label} {value!r}; must be a non-negative int")


def _classify(
    local_exists: bool, local_txid: int | None, remote: Remote
) -> tuple[State, ReasonCode, str]:
    if isinstance(remote, RemoteTxid):
        remote_txid: int | None = remote.txid
        remote_text = f"txid {remote_txid}"
    else:
        remote_txid = None
        remote_text = "empty" if isinstance(remote, RemoteEmpty) else f"error: {remote.message}"

    # 표의 순서대로 본다(DESIGN §4-3).
    # DB 없이 메타만 남은 경우는 원격 결과와 무관하게 stale_meta 다.
    if not local_exists and local_txid is not None:
        return (
            State.UNKNOWN,
            ReasonCode.STALE_META,
            f"no local db but local meta has txid {local_txid}; remote {remote_text}",
        )
    if isinstance(remote, RemoteError):
        where = "local db exists" if local_exists else "no local db"
        return (
            State.UNKNOWN,
            ReasonCode.REMOTE_ERROR,
            f"{where}, local txid {local_txid}; remote lookup failed: {remote.message}",
        )
    if not local_exists:
        if remote_txid is None:
            return State.FRESH, ReasonCode.NEW_DB, "no local db and remote empty; start new db"
        return (
            State.FRESH,
            ReasonCode.RESTORE_FROM_REMOTE,
            f"no local db; restore remote txid {remote_txid}",
        )

    if local_txid is None:
        return (
            State.UNKNOWN,
            ReasonCode.NO_LOCAL_META,
            f"local db exists but local meta is missing; remote {remote_text}",
        )
    if remote_txid is None:
        return (
            State.UNKNOWN,
            ReasonCode.REMOTE_EMPTY,
            f"local db exists with txid {local_txid} but remote is empty",
        )
    if local_txid >= remote_txid:
        return (
            State.MATCH,
            ReasonCode.LOCAL_CURRENT,
            f"local txid {local_txid} >= remote txid {remote_txid}",
        )
    return (
        State.UNKNOWN,
        ReasonCode.REMOTE_AHEAD,
        f"local txid {local_txid} < remote txid {remote_txid}",
    )


def decide(
    *,
    local_exists: bool,
    local_txid: int | None,
    remote: Remote,
    on_unknown: OnUnknown | str = OnUnknown.REFUSE,
) -> Decision:
    """부팅 상태(fresh/match/unknown)와 조치를 정한다. 규칙표는 DESIGN §4-3."""
    if not isinstance(local_exists, bool):
        raise ValueError(f"invalid local_exists {local_exists!r}; must be a bool")
    if local_txid is not None:
        _check_txid("local txid", local_txid)
    if not isinstance(remote, RemoteError | RemoteEmpty | RemoteTxid):
        raise ValueError(f"invalid remote {remote!r}; expected RemoteError/RemoteEmpty/RemoteTxid")
    if isinstance(remote, RemoteTxid):
        # dataclass 는 생성 후에도 object.__setattr__ 로 바뀔 수 있으므로 여기서 다시 본다.
        _check_txid("remote txid", remote.txid)
    try:
        policy = OnUnknown(on_unknown)
    except ValueError:
        choices = ", ".join(p.value for p in OnUnknown)
        raise ValueError(f"invalid on_unknown {on_unknown!r}; expected one of {choices}") from None

    state, code, reason = _classify(local_exists, local_txid, remote)

    if state is State.FRESH:
        action = Action.PROCEED if code is ReasonCode.NEW_DB else Action.RESTORE
    elif state is State.MATCH:
        action = Action.PROCEED
    elif policy is OnUnknown.RESTORE:
        # 복원할 복제본이 있을 때만 격리 후 복원한다. 원격 실패·빈 목록이면 거부한다.
        action = Action.QUARANTINE_AND_RESTORE if isinstance(remote, RemoteTxid) else Action.REFUSE
    elif policy is OnUnknown.KEEP_LOCAL:
        # 로컬 DB 가 없으면 지킬 것이 없고, 새 DB 로 시작하면 원격을 덮을 수 있다.
        action = Action.KEEP_LOCAL if local_exists else Action.REFUSE
    else:
        action = Action.REFUSE

    return Decision(state=state, action=action, reason_code=code, reason=reason)
