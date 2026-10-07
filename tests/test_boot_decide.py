"""``boot/decide.py`` 판정 함수 테스트 (DESIGN §4-3, §4-4, §11).

§4-4 의 스파이크 결함 네 가지 중 이 모듈 몫은 두 가지다(원격 실패·빈 목록 → 로컬 유지,
메타 없음 → 복원). 나머지 둘 — 격리를 디렉터리 하나로 원자적으로 옮기기(#4)와
``litestream ltx -level all`` 로 모든 레벨 보기(#3) — 는 판정 함수 밖의 일이라 각 이슈에서 막는다.
"""

import dataclasses
import itertools

import pytest
from _nodjango import assert_runs_without_django

from django_sqlite_ops.boot.decide import (
    Action,
    Decision,
    OnUnknown,
    ReasonCode,
    RemoteEmpty,
    RemoteError,
    RemoteTxid,
    State,
    decide,
)

ERR = RemoteError("timeout")
EMPTY = RemoteEmpty()
REMOTES = {"err": ERR, "empty": EMPTY, "3": RemoteTxid(3), "5": RemoteTxid(5), "7": RemoteTxid(7)}
POLICIES = ("refuse", "restore", "keep-local")

F, M, U = State.FRESH, State.MATCH, State.UNKNOWN
GO, RST, QR, KEEP, NO = (
    Action.PROCEED,
    Action.RESTORE,
    Action.QUARANTINE_AND_RESTORE,
    Action.KEEP_LOCAL,
    Action.REFUSE,
)
C = ReasonCode

# 손으로 쓴 기대값 표.
# (로컬 DB, 로컬 TXID, 원격) → (state, reason_code, refuse·restore·keep-local 각각의 action)
# adopt_existing=False 일 때의 표다. 판정 로직을 다시 계산하지 않는다.
# 표를 바꿀 때는 DESIGN §4-3 의 규칙표와 함께 바꾼다.
TABLE = {
    # 로컬 DB 없음, 메타 없음
    (False, None, "err"): (U, C.REMOTE_ERROR, NO, NO, NO),
    (False, None, "empty"): (F, C.NEW_DB, GO, GO, GO),
    (False, None, "3"): (F, C.RESTORE_FROM_REMOTE, RST, RST, RST),
    (False, None, "5"): (F, C.RESTORE_FROM_REMOTE, RST, RST, RST),
    (False, None, "7"): (F, C.RESTORE_FROM_REMOTE, RST, RST, RST),
    # 로컬 DB 없음, 메타만 남음
    (False, 5, "err"): (U, C.STALE_META, NO, NO, NO),
    (False, 5, "empty"): (U, C.STALE_META, NO, NO, NO),
    (False, 5, "3"): (U, C.STALE_META, NO, QR, NO),
    (False, 5, "5"): (U, C.STALE_META, NO, QR, NO),
    (False, 5, "7"): (U, C.STALE_META, NO, QR, NO),
    # 로컬 DB 있음, 메타 없음
    (True, None, "err"): (U, C.REMOTE_ERROR, NO, NO, NO),  # D-12
    (True, None, "empty"): (U, C.NO_LOCAL_META, NO, NO, KEEP),
    (True, None, "3"): (U, C.NO_LOCAL_META, NO, QR, KEEP),
    (True, None, "5"): (U, C.NO_LOCAL_META, NO, QR, KEEP),
    (True, None, "7"): (U, C.NO_LOCAL_META, NO, QR, KEEP),
    # 로컬 DB 있음, 로컬 TXID 5
    (True, 5, "err"): (U, C.REMOTE_ERROR, NO, NO, NO),  # D-12
    (True, 5, "empty"): (U, C.REMOTE_EMPTY, NO, NO, KEEP),
    (True, 5, "3"): (M, C.LOCAL_CURRENT, GO, GO, GO),
    (True, 5, "5"): (M, C.LOCAL_CURRENT, GO, GO, GO),
    (True, 5, "7"): (U, C.REMOTE_AHEAD, NO, QR, KEEP),
}

CASES = [
    pytest.param(local, txid, remote, policy, id=f"db={local}-txid={txid}-remote={remote}-{policy}")
    for (local, txid, remote), _ in TABLE.items()
    for policy in POLICIES
]
ALL_INPUTS = list(itertools.product((False, True), (None, 5), REMOTES, POLICIES))


# adopt_existing=True 가 효과를 내는 유일한 행(D-11). 나머지는 위 표와 같다.
ADOPT_ROW = (True, None, "empty")
ADOPT_EXPECTED = ("adopt", "adopt_existing", GO, GO, GO)


def _decide(local, txid, remote, policy, **kw):
    return decide(
        local_exists=local, local_txid=txid, remote=REMOTES[remote], on_unknown=policy, **kw
    )


def test_table_covers_every_combination():
    assert len(CASES) == len(ALL_INPUTS) == 60
    assert set(TABLE) == {(local, txid, r) for local, txid, r, _ in ALL_INPUTS}


@pytest.mark.parametrize(("local", "txid", "remote", "policy"), CASES)
def test_decision_table(local, txid, remote, policy):
    state, code, *actions = TABLE[(local, txid, remote)]
    expected = (state, code, actions[POLICIES.index(policy)])
    got = _decide(local, txid, remote, policy)
    assert (got.state, got.reason_code, got.action) == expected


# --- 안전 불변식: 전수 조합에 대해 ---


@pytest.mark.parametrize(("local", "txid", "remote", "policy"), ALL_INPUTS)
def test_invariant_existing_local_is_never_overwritten_without_quarantine(
    local, txid, remote, policy
):
    if local:
        assert _decide(local, txid, remote, policy).action is not Action.RESTORE


@pytest.mark.parametrize("adopt", [False, True])
@pytest.mark.parametrize(("local", "txid", "remote", "policy"), ALL_INPUTS)
def test_invariant_remote_error_always_refuses(local, txid, remote, policy, adopt):
    # D-12: 원격 실패면 정책(keep-local 포함)과 무관하게 거부한다.
    if remote == "err":
        assert _decide(local, txid, remote, policy, adopt_existing=adopt).action is Action.REFUSE


@pytest.mark.parametrize(("local", "txid", "remote", "policy"), ALL_INPUTS)
def test_invariant_refuse_policy_refuses_every_unknown(local, txid, remote, policy):
    got = _decide(local, txid, remote, "refuse")
    if got.state is State.UNKNOWN:
        assert got.action is Action.REFUSE


# --- adopt_existing (D-11) ---


@pytest.mark.parametrize("policy", POLICIES)
def test_adopt_existing_row(policy):
    state, code, *actions = ADOPT_EXPECTED
    got = _decide(*ADOPT_ROW, policy, adopt_existing=True)
    assert (got.state, got.reason_code, got.action) == (
        state,
        code,
        actions[POLICIES.index(policy)],
    )


@pytest.mark.parametrize(("local", "txid", "remote", "policy"), ALL_INPUTS)
def test_adopt_existing_has_no_effect_elsewhere(local, txid, remote, policy):
    if (local, txid, remote) == ADOPT_ROW:
        return
    on = _decide(local, txid, remote, policy, adopt_existing=True)
    off = _decide(local, txid, remote, policy, adopt_existing=False)
    assert on == off


def test_adopt_existing_default_is_off():
    got = _decide(*ADOPT_ROW, "refuse")
    assert (got.state, got.reason_code, got.action) == (U, C.NO_LOCAL_META, NO)


@pytest.mark.parametrize("adopt", [False, True])
@pytest.mark.parametrize(("local", "txid", "remote", "policy"), ALL_INPUTS)
def test_invariant_adopt_only_without_replica_meta_or_error(local, txid, remote, policy, adopt):
    got = _decide(local, txid, remote, policy, adopt_existing=adopt)
    if remote not in {"empty"} or txid is not None or not local:
        assert got.state != "adopt"
        assert got.reason_code != "adopt_existing"


@pytest.mark.parametrize("adopt", [None, 0, 1, "yes"])
def test_invalid_adopt_existing(adopt):
    with pytest.raises(ValueError, match="adopt_existing"):
        _decide(*ADOPT_ROW, "refuse", adopt_existing=adopt)


# --- §4-4 스파이크 결함 회귀 ---


def test_spike_regression_remote_error_does_not_keep_local():
    # 스파이크는 원격 조회 실패를 "로컬 유지, exit 0"으로 처리했다(L4).
    got = decide(local_exists=True, local_txid=5, remote=RemoteError("connection refused"))
    assert (got.state, got.action, got.reason_code) == (U, NO, C.REMOTE_ERROR)


def test_spike_regression_remote_empty_does_not_keep_local():
    # 스파이크는 빈 목록도 "로컬 유지"로 처리했다. 빈 목록은 설정 오류·경로 오타일 수 있다.
    got = decide(local_exists=True, local_txid=5, remote=RemoteEmpty())
    assert (got.state, got.action, got.reason_code) == (U, NO, C.REMOTE_EMPTY)


def test_spike_regression_missing_meta_does_not_restore():
    # 스파이크는 메타가 없으면 원격으로 복원해 더 새로운 로컬을 버렸다(L3).
    got = decide(local_exists=True, local_txid=None, remote=RemoteTxid(7))
    assert (got.state, got.action, got.reason_code) == (U, NO, C.NO_LOCAL_META)


def test_old_volume_reboot_is_refused():
    # L2(D4): 옛 볼륨으로 재부팅하면 원격이 앞선다.
    got = decide(local_exists=True, local_txid=5, remote=RemoteTxid(7))
    assert (got.state, got.action, got.reason_code) == (U, NO, C.REMOTE_AHEAD)


def test_unreplicated_local_commits_are_kept():
    # L5: 복제되지 않은 로컬 커밋이 있으면 로컬이 앞선다.
    got = decide(local_exists=True, local_txid=7, remote=RemoteTxid(5))
    assert (got.state, got.action) == (M, GO)


# --- 입력 검증 ---


@pytest.mark.parametrize("txid", [-1, True, False, 1.0, "5"])
def test_invalid_local_txid(txid):
    with pytest.raises(ValueError, match="local txid"):
        decide(local_exists=True, local_txid=txid, remote=EMPTY)


@pytest.mark.parametrize("txid", [-1, True, False, 1.0, "5", None])
def test_invalid_remote_txid(txid):
    with pytest.raises(ValueError, match="remote txid"):
        RemoteTxid(txid)


class _NeverNegative(int):
    def __lt__(self, other):
        return False


class _AlwaysAhead(int):
    def __ge__(self, other):
        return True


class _AmbiguousError(RemoteError):
    # isinstance(x, RemoteTxid) 도 참이 되는 실패 객체 (리뷰 1-2)
    @property
    def __class__(self):
        return RemoteTxid

    @property
    def txid(self):
        return 7


def test_txid_int_subclass_cannot_bypass_negative_check():
    with pytest.raises(ValueError, match="remote txid"):
        RemoteTxid(_NeverNegative(-1))


def test_txid_int_subclass_cannot_override_comparison():
    with pytest.raises(ValueError, match="local txid"):
        decide(local_exists=True, local_txid=_AlwaysAhead(1), remote=RemoteTxid(7))


def test_remote_txid_int_subclass_rejected_in_decide():
    remote = RemoteTxid(3)
    object.__setattr__(remote, "txid", _NeverNegative(-1))
    with pytest.raises(ValueError, match="remote txid"):
        decide(local_exists=True, local_txid=5, remote=remote)


def test_ambiguous_remote_cannot_authorize_restore():
    remote = _AmbiguousError("timeout")
    assert isinstance(remote, RemoteError) and isinstance(remote, RemoteTxid)
    with pytest.raises(ValueError, match="remote"):
        decide(local_exists=True, local_txid=5, remote=remote, on_unknown="restore")


@pytest.mark.parametrize("cls", [RemoteError, RemoteEmpty, RemoteTxid])
def test_remote_subclass_rejected(cls):
    sub = type("Sub", (cls,), {})
    remote = sub("x") if cls is RemoteError else sub(3) if cls is RemoteTxid else sub()
    with pytest.raises(ValueError, match="remote"):
        decide(local_exists=True, local_txid=5, remote=remote)


class _Str(str):
    pass


@pytest.mark.parametrize("message", [None, 403, b"x", _Str("x")])
def test_invalid_remote_error_message(message):
    with pytest.raises(ValueError, match="message"):
        RemoteError(message)


def test_remote_txid_mutated_after_creation_is_rejected():
    remote = RemoteTxid(3)
    object.__setattr__(remote, "txid", -1)
    with pytest.raises(ValueError, match="remote txid"):
        decide(local_exists=True, local_txid=5, remote=remote)


@pytest.mark.parametrize("policy", ["Refuse", "keep_local", "", None, 1])
def test_invalid_on_unknown(policy):
    with pytest.raises(ValueError, match="on_unknown"):
        decide(local_exists=True, local_txid=5, remote=EMPTY, on_unknown=policy)


@pytest.mark.parametrize("remote", [None, 7, "timeout"])
def test_invalid_remote(remote):
    with pytest.raises(ValueError, match="remote"):
        decide(local_exists=True, local_txid=5, remote=remote)


@pytest.mark.parametrize("local", [None, 1, "yes"])
def test_invalid_local_exists(local):
    with pytest.raises(ValueError, match="local_exists"):
        decide(local_exists=local, local_txid=None, remote=EMPTY)


def test_on_unknown_accepts_enum_and_defaults_to_refuse():
    kw = {"local_exists": True, "local_txid": 5, "remote": RemoteTxid(7)}
    assert decide(**kw).action is NO
    assert decide(**kw, on_unknown=OnUnknown.KEEP_LOCAL).action is KEEP


def test_txid_zero_is_valid():
    got = decide(local_exists=True, local_txid=0, remote=RemoteTxid(0))
    assert got.state is M


# --- 출력 ---


def test_decision_is_immutable():
    got = decide(local_exists=False, local_txid=None, remote=EMPTY)
    assert isinstance(got, Decision)
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.action = Action.REFUSE  # type: ignore[misc]


def test_reason_codes_are_stable_strings():
    assert {c.value for c in ReasonCode} == {
        "new_db",
        "restore_from_remote",
        "local_current",
        "adopt_existing",
        "remote_error",
        "stale_meta",
        "no_local_meta",
        "remote_empty",
        "remote_ahead",
    }
    assert {a.value for a in Action} == {
        "proceed",
        "restore",
        "quarantine_and_restore",
        "keep_local",
        "refuse",
    }


@pytest.mark.parametrize(
    ("local", "txid", "remote", "numbers"),
    [
        (True, 41, RemoteTxid(97), ["41", "97"]),
        (True, 97, RemoteTxid(41), ["97", "41"]),
        (True, 41, RemoteEmpty(), ["41"]),
        (True, None, RemoteTxid(97), ["97"]),
        (False, 41, RemoteTxid(97), ["41", "97"]),
        (False, None, RemoteTxid(97), ["97"]),
        (True, 41, RemoteError("HTTP 403 Forbidden"), ["41", "HTTP 403 Forbidden"]),
    ],
)
def test_reason_contains_numbers(local, txid, remote, numbers):
    reason = decide(local_exists=local, local_txid=txid, remote=remote).reason
    assert "\n" not in reason
    for n in numbers:
        assert n in reason


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("txid", [None, 5])
@pytest.mark.parametrize(
    "message", ["HTTP 403\nrequest failed\rretry", "a\r\nb", "a\x0bb\x0cc\x1cd\u2028e\x85f"]
)
def test_reason_is_single_line_for_multiline_error(local, txid, message):
    # 리뷰 1-3: remote_error 와 stale_meta 경로 모두 한 줄이어야 한다.
    reason = decide(local_exists=local, local_txid=txid, remote=RemoteError(message)).reason
    assert len(reason.splitlines()) == 1, reason


# --- Django 비의존 (DESIGN §4-1) ---


def test_import_without_django():
    assert_runs_without_django(
        """
        import django_sqlite_ops.boot
        from django_sqlite_ops.boot.decide import RemoteEmpty, decide

        decide(local_exists=False, local_txid=None, remote=RemoteEmpty())
        """
    )
