"""회귀 랩(lab/, #10)의 Docker 없이 확인할 수 있는 약속.

- 랩 시나리오는 ``RUN_LAB=1`` 일 때만 수집된다. 기본 ``pytest`` 는 Docker 가 필요 없다.
- 랩 빌드 컨텍스트는 배포 프로필 문서의 조각에서 만든다. 문서와 다른 줄은 ``SUBSTITUTIONS`` 뿐이다.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _build_context():
    spec = importlib.util.spec_from_file_location(
        "build_context", ROOT / "lab" / "build_context.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lab_is_not_collected_by_default():
    env = {k: v for k, v in os.environ.items() if k != "RUN_LAB"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "lab"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 5, proc.stdout + proc.stderr  # 5 = no tests collected
    default = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    collected = {line.split("::")[0] for line in default.stdout.splitlines() if "::" in line}
    assert collected and not [f for f in collected if f.startswith("lab/")]


def test_build_context_differs_from_docs_only_by_substitutions(tmp_path):
    bc = _build_context()
    wheel = tmp_path / "django_sqlite_ops-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    build = tmp_path / "build"
    bc.main(str(wheel), build)

    doc = (ROOT / "docs" / "profiles" / "single-server.md").read_text(encoding="utf-8")
    original = re.findall(r"^```dockerfile\n(.*?)^```", doc, flags=re.S | re.M)[0]
    built = (build / "Dockerfile").read_text(encoding="utf-8")
    changed = [
        (a, b) for a, b in zip(original.splitlines(), built.splitlines(), strict=True) if a != b
    ]
    assert changed == [("COPY requirements.txt .", f"COPY requirements.txt {wheel.name} ./")]

    # settings·urls 는 문서 조각 그대로에 랩 추가분을 붙인 것이다
    settings = (build / "proj" / "settings.py").read_text(encoding="utf-8")
    snippet = next(
        b for b in re.findall(r"^```python\n(.*?)^```", doc, flags=re.S | re.M) if "SQLITE_OPS" in b
    )
    assert settings.startswith(snippet)
    assert os.access(build / "entrypoint.sh", os.X_OK)
    lite = (build / "litestream.yml").read_text(encoding="utf-8")
    assert "endpoint: http://toxiproxy:18333" in lite and "path: ${LAB_PREFIX}" in lite
    # P2(#66): 문서 Dockerfile 은 그대로, 그 이미지 위에 [nats] extra 를 판 고정으로 얹는다
    nats_req = (build / "requirements-nats.txt").read_text(encoding="utf-8").splitlines()
    assert nats_req == [
        f"django-sqlite-ops[nats] @ file:///app/{wheel.name}",
        f"channels-nats=={bc.CHANNELS_NATS}",
    ]
    assert (build / "nats.Dockerfile").read_text(encoding="utf-8").count("FROM ${BASE}") == 1


# --- 랩 판정 함수(lab/_checks.py) — Docker 없이 -------------------------------------------


def _checks():
    spec = importlib.util.spec_from_file_location("lab_checks", ROOT / "lab" / "_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


META = ".app.sqlite3-litestream"
LTX = f"{META}/ltx/0/0000000000000002-0000000000000002.ltx"


def _template():
    return {
        "entries": {
            "app.sqlite3": {"type": "file", "sha256": "db"},
            "app.sqlite3-wal": {"type": "file", "sha256": "wal"},
            "app.sqlite3-shm": {"type": "file", "sha256": "shm"},
            "app.sqlite3.boot.lock": {"type": "file"},
            "app.sqlite3.boot-state.json": {"type": "file", "sha256": "state"},
            META: {"type": "dir"},
            f"{META}/ltx": {"type": "dir"},
            f"{META}/ltx/0": {"type": "dir"},
            LTX: {"type": "file", "sha256": "ltx"},
        }
    }


def _final(*stales):
    """재실행 뒤 볼륨. 새 DB·메타가 제자리에 있고, 격리 디렉터리는 ``(이름, {상대경로: 항목})``."""
    entries = {
        "app.sqlite3": {"type": "file", "sha256": "new-db"},
        "app.sqlite3-wal": {"type": "file", "sha256": "new-wal"},
        META: {"type": "dir"},
        f"{META}/ltx": {"type": "dir"},
    }
    for name, content in stales:
        entries[name] = {"type": "dir"}
        for rel, e in content.items():
            entries[f"{name}/{rel}"] = e
    return {"entries": entries}


def _moved(template, drop=()):
    c = _checks()
    fp = c.template_fingerprint(template)
    content = {"manifest.json": {"type": "file", "sha256": "manifest"}}
    for rel in fp:
        if rel not in drop:
            content[rel] = template["entries"][rel]
    return content


def test_l6_fingerprint_covers_db_sidecars_and_meta_tree():
    fp = _checks().template_fingerprint(_template())
    assert fp == {
        "app.sqlite3": "db",
        "app.sqlite3-wal": "wal",
        "app.sqlite3-shm": "shm",
        META: "dir",
        f"{META}/ltx": "dir",
        f"{META}/ltx/0": "dir",
        LTX: "ltx",
    }


def test_l6_complete_quarantine_passes():
    c = _checks()
    t = _template()
    fp = c.template_fingerprint(t)
    final = _final(("app.sqlite3.stale-1", _moved(t)))
    assert c.quarantine_problems(final, fp, expected_stales=1) == []


def test_l6_empty_meta_dir_fails():
    """리뷰 1 의 합성 상태: 옛 DB 와 메타 디렉터리는 함께 있지만 메타가 비었다."""
    c = _checks()
    t = _template()
    fp = c.template_fingerprint(t)
    content = _moved(t, drop={f"{META}/ltx", f"{META}/ltx/0", LTX})
    problems = c.quarantine_problems(_final(("app.sqlite3.stale-1", content)), fp, 1)
    assert any(LTX in p and "missing" in p for p in problems), problems


def test_l6_sidecars_in_another_stale_dir_fail():
    """리뷰 1 의 합성 상태: -wal·-shm 이 다른 격리 디렉터리에 갈라져 있다."""
    c = _checks()
    t = _template()
    fp = c.template_fingerprint(t)
    main = _moved(t, drop={"app.sqlite3-wal", "app.sqlite3-shm"})
    split = {
        "manifest.json": {"type": "file", "sha256": "m2"},
        "app.sqlite3-wal": t["entries"]["app.sqlite3-wal"],
        "app.sqlite3-shm": t["entries"]["app.sqlite3-shm"],
    }
    final = _final(("app.sqlite3.stale-1", main), ("app.sqlite3.stale-2", split))
    for expected in (1, 2):
        problems = c.quarantine_problems(final, fp, expected)
        assert any("app.sqlite3-wal missing" in p for p in problems), problems
        assert any("outside" in p for p in problems), problems


def test_l6_changed_content_and_leftover_partial_fail():
    c = _checks()
    t = _template()
    fp = c.template_fingerprint(t)
    content = _moved(t)
    content["app.sqlite3"] = {"type": "file", "sha256": "other"}
    final = _final(("app.sqlite3.stale-1", content))
    final["entries"]["app.sqlite3.stale-9.partial"] = {"type": "dir"}
    problems = c.quarantine_problems(final, fp, 1)
    assert any("differs" in p for p in problems), problems
    assert any(".partial" in p for p in problems), problems


def test_l6_d14_requarantine_layout():
    """설치 뒤 kill(D-14): 두 번째 격리 디렉터리는 설치됐던 복원본만 담는다."""
    c = _checks()
    t = _template()
    fp = c.template_fingerprint(t)
    second = {
        "manifest.json": {"type": "file", "sha256": "m2"},
        "app.sqlite3": {"type": "file", "sha256": "restored"},
        "app.sqlite3-shm": {"type": "file", "sha256": "restored-shm"},
    }
    final = _final(("app.sqlite3.stale-1", _moved(t)), ("app.sqlite3.stale-2", second))
    assert c.quarantine_problems(final, fp, expected_stales=2) == []
    assert c.quarantine_problems(final, fp, expected_stales=1)  # 개수가 기대와 다르다

    with_meta = dict(second, **{META: {"type": "dir"}})
    final = _final(("app.sqlite3.stale-1", _moved(t)), ("app.sqlite3.stale-2", with_meta))
    assert any("meta" in p for p in c.quarantine_problems(final, fp, expected_stales=2))


def _real_health_timeline(
    observations: list[float], outage: float = 20, times: list[float] | None = None
):
    """실제 ``health.alias_status`` 로 만든 단절 중 표본(1초 간격, 0.1초부터).

    ``observations`` 는 성공한 조회가 끝난 시각(단절 시작 = 0초)이다. 각 표본 시각에는 그때까지
    끝난 마지막 관측이 보인다.
    """
    from django_sqlite_ops.boot.decide import RemoteTxid
    from django_sqlite_ops.health import Sample, alias_status
    from django_sqlite_ops.wal import IN_SYNC, WalEvidence

    timeline = []
    if times is None:
        times = [0.1 + i for i in range(int(outage))]
    for t in times:
        observed = max(o for o in observations if o <= t)
        sample = Sample(
            observed=observed,
            checked_at=0,
            local=1,
            remote=RemoteTxid(1),
            wal=WalEvidence(IN_SYNC, "in sync"),
        )
        r = alias_status(sample, t, refresh=2, grace=10)
        timeline.append((round(t, 4), r["status"], r["code"], r["age"]))
    return timeline


def test_l8a_refresh_just_before_outage_passes():
    """리뷰 1: 단절 직전에 갱신됐으면 5.1초에도 caught_up 이 정상이다."""
    c = _checks()
    timeline = _real_health_timeline([0.0])
    assert timeline[5][:2] == (5.1, "caught_up")
    assert c.full_outage_problems(timeline, refresh=2) == []


def test_l8a_inflight_lookup_finishing_after_cut_passes():
    """단절 직전에 시작한 조회가 단절 1.5초 뒤 성공으로 끝나도 제한 안에 stale 이 된다."""
    c = _checks()
    timeline = _real_health_timeline([-2.0, 1.5])
    assert c.full_outage_problems(timeline, refresh=2) == []


def test_l8a_never_stale_or_too_late_fails():
    c = _checks()
    never = [(t + 0.1, "caught_up", "in_sync", 1.0) for t in range(20)]
    assert c.full_outage_problems(never, refresh=2)
    # 단절 뒤에도 6초까지 조회가 성공했다면(끊기지 않은 것) stale 은 12초 뒤에야 보인다
    late = _real_health_timeline([0.0, 2.0, 4.0, 6.0])
    assert any("later than" in p for p in c.full_outage_problems(late, refresh=2))


def test_l8a_returning_to_caught_up_during_outage_fails():
    c = _checks()
    timeline = _real_health_timeline([0.0])
    timeline[12] = (timeline[12][0], "caught_up", "in_sync", 0.5)
    problems = c.full_outage_problems(timeline, refresh=2)
    assert any("left unknown" in p for p in problems), problems


def test_l8a_rounded_age_at_threshold_passes():
    """리뷰 2: 마지막 관측 6.0004초 뒤 실제 응답은 stale 인데 age 는 6.0 으로 반올림된다."""
    c = _checks()
    times = [0.1, 1.1, 2.1, 3.1, 4.1, 5.1, 6.0004] + [7.1 + i for i in range(13)]
    timeline = _real_health_timeline([0.0], times=times)
    assert timeline[6][1:] == ("unknown", "stale", 6.0)
    assert c.full_outage_problems(timeline, refresh=2) == []


def test_l8a_clearly_young_stale_fails():
    c = _checks()
    timeline = _real_health_timeline([0.0])
    timeline[3] = (timeline[3][0], "unknown", "stale", 5.0)
    problems = c.full_outage_problems(timeline, refresh=2)
    assert any("age 5.0 < 6" in p for p in problems), problems


def _long_outage_timeline(
    last_ok: float, first_fail_start: float | None = None, outage: float = 90, timeout=30.0
):
    """실제 ``health.alias_status`` 로 만든 긴 단절 표본(1초 간격, 0.1초부터, strict HTTP 포함).

    갱신 스레드처럼: 마지막 성공 관측 ``last_ok`` 뒤 ``REFRESH``(2초)를 쉬고 시작한 조회가
    ``timeout`` 뒤 ``RemoteError`` 로 끝나고, 다시 2초 쉬고 조회한다(``first_fail_start`` 로 첫
    실패 조회의 시작을 바꿀 수 있다).
    """
    from django_sqlite_ops.boot.decide import RemoteError, RemoteTxid
    from django_sqlite_ops.health import Sample, alias_status
    from django_sqlite_ops.wal import IN_SYNC, WalEvidence

    observed = [(last_ok, True)]
    start = last_ok + 2 if first_fail_start is None else first_fail_start
    while start + timeout <= outage + timeout:
        observed.append((start + timeout, False))
        start += timeout + 2
    timeline = []
    for t in [0.1 + i for i in range(int(outage))]:
        at, ok = max(o for o in observed if o[0] <= t)
        sample = Sample(
            observed=at,
            checked_at=0,
            local=1,
            remote=RemoteTxid(1) if ok else RemoteError("litestream ltx timed out after 30s"),
            wal=WalEvidence(IN_SYNC, "in sync"),
        )
        r = alias_status(sample, t, refresh=2, grace=10)
        http = 200 if r["status"] == "caught_up" else 503
        timeline.append((round(t, 4), r["status"], r["code"], r["age"], http))
    return timeline


def test_l8_long_expected_oscillation_passes():
    """#65: 30초 타임아웃 조회가 반복되면 remote_error(약 6초)와 stale 이 32초 주기로 번갈아
    나온다."""
    c = _checks()
    timeline = _long_outage_timeline(last_ok=0.0)
    assert c.error_episodes(timeline) == [32.1, 64.1]
    assert {code for _t, _s, code, _a, _h in timeline} >= {"in_sync", "stale", "remote_error"}
    assert c.long_outage_problems(timeline, refresh=2, outage=90) == []
    # 단절 직전에 시작해 진행 중이던 조회가 첫 실패(29초 남짓)여도 통과한다
    early = _long_outage_timeline(last_ok=-1.5, first_fail_start=-0.5)
    assert c.long_outage_problems(early, refresh=2, outage=90) == []


def test_l8_long_fast_failure_or_no_error_fails():
    c = _checks()
    # ltx 가 곧바로 실패하면(타임아웃에 매달리지 않음) 기대와 다르다
    fast = _long_outage_timeline(last_ok=0.0, first_fail_start=2.0, timeout=3.0)
    problems = c.long_outage_problems(fast, refresh=2, outage=90)
    assert any("first remote_error" in p for p in problems), problems
    # 조회가 끝나지 않아 stale 만 계속되면(타임아웃이 듣지 않음) 실패
    stuck = _long_outage_timeline(last_ok=0.0, timeout=500.0)
    assert "never became remote_error" in c.long_outage_problems(stuck, refresh=2, outage=90)


def test_l8_long_wrong_http_or_gap_fails():
    c = _checks()
    timeline = _long_outage_timeline(last_ok=0.0)
    bad_http = [row[:4] + (200,) if row[2] == "stale" else row for row in timeline]
    assert any("strict HTTP 200" in p for p in c.long_outage_problems(bad_http, 2, 90))
    # 두 번째 remote_error 가 없으면(구간 수 부족) 실패
    one = [row if row[0] < 60 else (row[0], "unknown", "stale", 7.0, 503) for row in timeline]
    assert any("episode(s)" in p for p in c.long_outage_problems(one, 2, 90))


def test_l8_ltx_process_checks():
    c = _checks()
    outage = [(t, [(100, "S")]) for t in range(0, 31, 3)] + [(33, []), (36, [(200, "S")])]
    after = [(1, []), (3, [(300, "S")])]
    assert c.ltx_process_problems(outage, after) == []
    two = outage + [(39, [(200, "S"), (201, "S")])]
    assert any("2 ltx processes" in p for p in c.ltx_process_problems(two, after))
    zombie = outage + [(39, [(200, "Z")])]
    assert any("zombie" in p for p in c.ltx_process_problems(zombie, after))
    long = [(t, [(100, "S")]) for t in range(0, 61, 3)]
    assert any("seen for 60" in p for p in c.ltx_process_problems(long, after))
    left = c.ltx_process_problems(outage, [(1, [(200, "S")])])
    assert any("still running" in p for p in left)


def test_l8_long_first_error_and_upper_bound_pairs():
    """리뷰 1 P3-1: 첫 remote_error 경계 쌍.

    하한 29초(28 실패·30 통과), 상한 36초(35 통과·37 실패).
    """
    c = _checks()

    def first_at(t0):
        timeline = _long_outage_timeline(last_ok=0.0, first_fail_start=t0 - 30)
        assert c.error_episodes(timeline)[0] == round(t0 + 0.1, 4)
        return any("first remote_error" in p for p in c.long_outage_problems(timeline, 2, 90))

    assert first_at(28.0) and not first_at(30.0)
    assert not first_at(35.0) and first_at(37.0)


def test_l8_long_remote_error_age_guard():
    """리뷰 1 P3-4: 판정 순서가 바뀌어 age > 문턱인 remote_error 가 나오면 잡는다(직접 만든 행)."""
    c = _checks()
    timeline = _long_outage_timeline(last_ok=0.0)
    i = next(i for i, row in enumerate(timeline) if row[2] == "stale" and row[0] > 40)
    timeline[i] = (timeline[i][0], "unknown", "remote_error", 7.0, 503)
    assert any("remote_error at" in p for p in c.long_outage_problems(timeline, 2, 90))


def test_l8_max_sample_gap():
    c = _checks()
    assert c.max_sample_gap([]) == 0.0
    assert c.max_sample_gap([(0.1, "x"), (1.1, "x"), (3.4, "x"), (4.4, "x")]) == 2.3


def test_l8_ltx_empty_samples_fail():
    """리뷰 1 P2-1: 검출기가 고장 나 빈 표본만 쌓이면 '매달린 ltx 없음'으로 통과하지 않는다."""
    c = _checks()
    problems = c.ltx_process_problems([(t, []) for t in range(0, 90, 3)], [(1, [])])
    assert any("outage sample(s)" in p for p in problems), problems
    assert any("distinct ltx pid(s)" in p for p in problems), problems
    one_pid = [(t, [(100, "S")]) for t in range(0, 28, 3)]
    assert any("1 distinct" in p for p in c.ltx_process_problems(one_pid, [(1, [])]))


def test_l8_ltx_pid_span_pair():
    """리뷰 1 P3-1: 한 PID 의 관측 상한 40초(39 통과·42 실패)."""
    c = _checks()

    def outage(span):
        first = [(float(t), [(100, "S")]) for t in range(0, span, 3)] + [
            (float(span), [(100, "S")])
        ]
        return first + [(span + 3.0, [(200, "S")]), (span + 6.0, [(200, "S")])]

    assert c.ltx_process_problems(outage(39), [(1, [])]) == []
    assert any("seen for 42" in p for p in c.ltx_process_problems(outage(42), [(1, [])]))


# --- PRAGMA 벤치 집계(#26, lab/_benchstat.py) ------------------------------------------------


# --- P2 채널 레이어 전달(#66) ----------------------------------------------------------------

CH_A, CH_B = "specific.a!x", "specific.b!y"


def _worker(pid, channel, ids, nats_log=(), cancel_times=(), errors=(), alive=None):
    return {
        "pid": pid,
        "channel": channel,
        "ids": list(ids),
        "nats_log": list(nats_log),
        "cancels": len(cancel_times),
        "cancel_times": list(cancel_times),
        "errors": list(errors),
        "read_loop_alive": alive,
    }


def _trial(*workers, errors=(), jobs=None, t0=10.0, t1=11.0):
    t = {"workers": list(workers), "errors": list(errors), "senders": [1], "t0": t0, "t1": t1}
    if jobs is not None:
        t["jobs"] = jobs
    return t


def _full(channel, dropped):
    return f"channels_nats: mailbox for {channel} is full (capacity 100); {dropped} message(s) dropped so far"  # noqa: E501


def _room(channel, dropped):
    return (
        f"channels_nats: mailbox for {channel} has room again after dropping {dropped} message(s)"  # noqa: E501
    )


def test_p2_nats_drops_sums_episodes_per_channel():
    c = _checks()
    lines = [_full(CH_A, 1), _full(CH_B, 1), _full(CH_A, 7), _room(CH_A, 9), _full(CH_A, 2)]
    # 구간 1 은 "room again" 의 9, 구간 2 는 끝까지 차 있어 마지막 누적 2. 다른 채널은 따로.
    assert c.nats_drops(lines, CH_A) == 11
    assert c.nats_drops(lines, CH_B) == 1
    assert c.nats_drops([], CH_A) == 0


def test_p2_each_requires_every_id_once_in_order():
    c = _checks()
    spec = {"n": 5, "expect": "each"}
    ok = _trial(_worker(1, CH_A, range(5)), _worker(2, CH_B, range(5)))
    assert c.delivery_problems(spec, ok) == []
    lost = _trial(_worker(1, CH_A, [0, 1, 2, 4]), _worker(2, CH_B, range(5)))
    assert any("received 4/5, lost 1" in p for p in c.delivery_problems(spec, lost))
    dup = _trial(_worker(1, CH_A, [0, 1, 1, 2, 3, 4]), _worker(2, CH_B, range(5)))
    assert any("duplicate" in p for p in c.delivery_problems(spec, dup))
    order = _trial(_worker(1, CH_A, [1, 0, 2, 3, 4]), _worker(2, CH_B, range(5)))
    assert any("out of order" in p for p in c.delivery_problems(spec, order))
    one = _trial(_worker(1, CH_A, range(5)))
    assert any("1 workers reported" in p for p in c.delivery_problems(spec, one))


def test_p2_errors_and_unknown_ids_always_fail():
    c = _checks()
    spec = {"n": 3, "expect": "record"}
    bad = _trial(
        _worker(1, "lab.plain.k", [0, 7], errors=["FlushTimeoutError: x"]),
        _worker(2, "lab.plain.k", [1]),
        errors=["0 NoServersError: y"],
    )
    problems = c.delivery_problems(spec, bad)
    assert any("send errors" in p for p in problems)
    assert any("receive errors" in p for p in problems)
    assert any("unknown ids [7]" in p for p in problems)


def test_p2_nats_read_loop_error_fails_every_expectation():
    """nats-py 읽기 루프가 끝났다는 줄이 있으면 손실을 허용하는 판정도 실패한다(#66 리뷰 P3-3)."""
    c = _checks()
    ch = "lab.plain.k"
    dead = "nats: encountered error [InvalidStateError: invalid state]"
    for expect in ("each", "split", "lossy", "drops", "record"):
        spec = {"n": 2, "expect": expect}
        trial = _trial(_worker(1, ch, [0], nats_log=[dead]), _worker(2, ch, [1]))
        problems = c.delivery_problems(spec, trial)
        assert any("read loop error" in p and "InvalidStateError" in p for p in problems), (
            expect,
            problems,
        )
    # 다른 경고(용량 드롭)만으로는 이 판정이 나지 않는다
    calm = _trial(_worker(1, ch, [0], nats_log=[_full(ch, 1)]), _worker(2, ch, [1]))
    assert not any(
        "read loop" in p for p in c.delivery_problems({"n": 2, "expect": "record"}, calm)
    )


DEAD = "nats: encountered error [InvalidStateError: invalid state]"


def test_p2_known_defect_is_excused_only_with_defect_ok():
    """defect_ok 시나리오만 InvalidStateError 로 끝난 읽기 루프를 결함으로 세고 넘긴다(#66)."""
    c = _checks()
    ch = "lab.plain.k"
    for expect in ("lossy", "record"):
        trial = _trial(_worker(1, ch, [0], nats_log=[DEAD], alive=False), _worker(2, ch, [1]))
        ok = {"n": 4, "expect": expect, "defect_ok": True}
        assert c.known_defect_pids(ok, trial) == [1]
        assert c.delivery_problems(ok, trial) == []
        # 같은 시도라도 defect_ok 가 없으면 실패다(기본 레이어 시나리오)
        plain = {"n": 4, "expect": expect}
        assert c.known_defect_pids(plain, trial) == []
        assert any("read loop error" in p for p in c.delivery_problems(plain, trial))
    # stop 뒤에 늦게 온 줄도 센다
    late = _trial(_worker(1, ch, [], alive=False), _worker(2, ch, [1]))
    late["workers"][0]["late_nats_log"] = [DEAD]
    assert c.known_defect_pids({"n": 2, "expect": "lossy", "defect_ok": True}, late) == [1]


def test_p2_known_defect_needs_a_dead_read_loop():
    """결함 줄이 있어도 그 워커의 레이어 인스턴스 읽기 루프가 살아 있으면 면제하지 않는다(#66 P3-2).

    nats 로그는 워커 프로세스 전역이라 다른 인스턴스의 늦은 줄이 시간 창에 섞일 수 있다.
    """
    c = _checks()
    ch = "lab.plain.k"
    spec = {"n": 4, "expect": "lossy", "defect_ok": True}
    for alive in (True, None):
        trial = _trial(_worker(1, ch, [0], nats_log=[DEAD], alive=alive), _worker(2, ch, [1]))
        assert c.known_defect_pids(spec, trial) == []
        assert any("read loop error" in p for p in c.delivery_problems(spec, trial))
    stuck = {**_trial(_worker(1, ch, [], nats_log=[DEAD], alive=True), _worker(2, ch, [])),
             "ready": False, "probes_sent": 9}  # fmt: skip
    assert any("not ready after 9" in p for p in c.delivery_problems(spec, stuck))
    # 거꾸로 읽기 루프가 끝났어도 결함 줄이 없으면 원인을 모르므로 면제하지 않는다
    stuck["workers"][0].update(nats_log=[], read_loop_alive=False)
    assert c.known_defect_pids(spec, stuck) == []
    assert any("not ready after 9" in p for p in c.delivery_problems(spec, stuck))


def test_p2_known_defect_keeps_the_rest_of_the_contract():
    """결함이 난 시도도 중복·용량 드롭·합 > n·다른 예외·결함 없는 준비 실패는 실패다."""
    c = _checks()
    ch = "lab.plain.k"
    spec = {"n": 4, "expect": "lossy", "defect_ok": True}
    dup = _trial(_worker(1, ch, [0, 0], nats_log=[DEAD], alive=False), _worker(2, ch, [1]))
    assert any("duplicate" in p for p in c.delivery_problems(spec, dup))
    dropped = _trial(
        _worker(1, ch, [0], nats_log=[DEAD, _full(ch, 2)], alive=False), _worker(2, ch, [])
    )
    assert any("mailbox drop" in p for p in c.delivery_problems(spec, dropped))
    # 다른 예외로 끝난 읽기 루프는 결함이 아니다 — 줄 하나라도 섞이면 실패
    other = "nats: encountered error [ValueError: bad]"
    for log in ([other], [DEAD, other]):
        trial = _trial(_worker(1, ch, [0], nats_log=log, alive=False), _worker(2, ch, [1]))
        assert c.known_defect_pids(spec, trial) == []
        assert any("read loop error" in p for p in c.delivery_problems(spec, trial))
    # 한 워커의 결함 면제는 다른 워커의 다른 예외로 끝난 읽기 루프에 닿지 않는다(#66 P3-1)
    mixed = _trial(_worker(1, ch, [0], nats_log=[DEAD], alive=False),
                   _worker(2, ch, [1], nats_log=[other], alive=False))  # fmt: skip
    assert c.known_defect_pids(spec, mixed) == [1]
    problems = c.delivery_problems(spec, mixed)
    assert any(p.startswith("pid 2: nats-py read loop error") for p in problems), problems
    assert not any(p.startswith("pid 1:") for p in problems), problems
    flush = ["FlushTimeoutError: x"]
    recv = _trial(_worker(1, ch, [0], nats_log=[DEAD], errors=flush, alive=False),
                  _worker(2, ch, [1]))  # fmt: skip
    assert any("receive errors" in p for p in c.delivery_problems(spec, recv))
    # 준비 실패: 결함이 있으면 기록만, 없으면 실패
    stuck = {**_trial(_worker(1, ch, []), _worker(2, ch, [])), "ready": False, "probes_sent": 9}
    assert any("not ready after 9" in p for p in c.delivery_problems(spec, stuck))
    stuck["workers"][0]["nats_log"] = [DEAD]
    stuck["workers"][0]["read_loop_alive"] = False
    assert c.delivery_problems(spec, stuck) == []
    stuck["workers"][0]["nats_log"] = [other]
    assert any("not ready" in p for p in c.delivery_problems(spec, stuck))


def test_p2_defect_ok_needs_its_own_layer():
    """defect_ok 면제는 별도 레이어 인스턴스에만 둔다(기본 레이어에 닿지 않게, #66 P3-1)."""
    c = _checks()
    specs = [
        {"id": "OK", "defect_ok": True, "layer": "x"},
        {"id": "PLAIN"},
        {"id": "BAD", "defect_ok": True},
        {"id": "EMPTY", "defect_ok": True, "layer": ""},
    ]
    assert c.channel_spec_problems(specs) == [
        "BAD: defect_ok needs its own layer",
        "EMPTY: defect_ok needs its own layer",
    ]


def test_p2_skip_after_only_on_a_known_defect_in_the_named_spec():
    """P-cancel-after 는 P-cancel 에서 알려진 결함이 났을 때만 건너뛴다(#66 P3-3)."""
    c = _checks()
    spec = {"id": "P-cancel-after", "after": "P-cancel"}
    hit = [{"trial": 0, "pids": [40]}]
    assert c.skip_after(spec, {"P-cancel": hit}) == "known defect in P-cancel"
    assert c.skip_after(spec, {}) is None
    assert c.skip_after(spec, {"P-cancel": []}) is None
    assert c.skip_after(spec, {"D2-cap100": hit}) is None
    assert c.skip_after({"id": "LIVE"}, {"P-cancel": hit}) is None


def test_p2_split_queue_group_needs_union_once_and_both_workers():
    c = _checks()
    spec = {"n": 4, "expect": "split"}
    ch = "lab.plain.k"
    assert c.delivery_problems(spec, _trial(_worker(1, ch, [0, 2]), _worker(2, ch, [1, 3]))) == []
    both = _trial(_worker(1, ch, [0, 1, 2]), _worker(2, ch, [2, 3]))
    assert any("more than one worker" in p for p in c.delivery_problems(spec, both))
    starved = _trial(_worker(1, ch, range(4)), _worker(2, ch, []))
    assert any("no share" in p for p in c.delivery_problems(spec, starved))
    lost = _trial(_worker(1, ch, [0]), _worker(2, ch, [3]))
    assert any("lost 2" in p for p in c.delivery_problems(spec, lost))


def test_p2_lossy_allows_loss_but_not_drops_or_duplicates():
    c = _checks()
    spec = {"n": 10, "expect": "lossy"}
    ch = "lab.plain.k"
    assert c.delivery_problems(spec, _trial(_worker(1, ch, [0, 4]), _worker(2, ch, [5]))) == []
    dropped = _trial(_worker(1, ch, [0], nats_log=[_full(ch, 3)]), _worker(2, ch, []))
    assert any("mailbox drop" in p for p in c.delivery_problems(spec, dropped))


def test_p2_drops_loss_must_equal_logged_drops():
    c = _checks()
    spec = {"n": 4, "expect": "drops"}
    ok = _trial(
        _worker(1, CH_A, [0, 1], nats_log=[_full(CH_A, 1), _room(CH_A, 2)]),
        _worker(2, CH_B, range(4)),
    )
    assert c.delivery_problems(spec, ok) == []
    silent = _trial(_worker(1, CH_A, [0, 1]), _worker(2, CH_B, range(4)))
    assert any("lost 2 != mailbox drops 0" in p for p in c.delivery_problems(spec, silent))


def test_p2_worker_sender_must_not_be_the_receiver():
    c = _checks()
    spec = {"n": 2, "expect": "each"}
    workers = (_worker(1, CH_A, range(2)), _worker(2, CH_B, range(2)))
    good = _trial(*workers, jobs=[{"to": CH_A, "sender": 2}, {"to": CH_B, "sender": 1}])
    assert c.delivery_problems(spec, good) == []
    self_send = _trial(*workers, jobs=[{"to": CH_A, "sender": 1}])
    assert any("its own channel" in p for p in c.delivery_problems(spec, self_send))


def test_p2_summary_counts_cancels_near_the_send_window():
    c = _checks()
    trial = _trial(
        _worker(1, "lab.plain.k", [0], cancel_times=[9.0, 9.96, 10.5, 11.04, 12.0]),
        _worker(2, "lab.plain.k", [1]),
    )
    s = c.delivery_summary({"n": 3}, trial)
    assert s["workers"][0]["cancels_near_send"] == 3
    assert s["union_lost"] == 1 and s["lost_ids"] == [2] and s["total"] == 2


def _benchstat():
    spec = importlib.util.spec_from_file_location("lab_benchstat", ROOT / "lab" / "_benchstat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(cma, variant, rep, rps, **extra):
    return {"cma": cma, "variant": variant, "rep": rep, "phases": {"mixed": {"rps": rps, **extra}}}


def test_bench_pairs_within_rep_and_cma():
    b = _benchstat()
    runs = [
        _run("0", "baseline", 1, 100.0),
        _run("0", "baseline", 2, 200.0),
        _run("none", "baseline", 1, 50.0),
        _run("0", "cache_size", 2, 180.0),
        _run("0", "cache_size", 1, 90.0),
        _run("none", "cache_size", 1, 60.0),
    ]
    assert b.paired_deltas(runs, "0", "cache_size", "mixed", "rps") == [-10.0, -10.0]
    assert b.paired_deltas(runs, "none", "cache_size", "mixed", "rps") == [20.0]


def test_bench_reproduced_needs_every_rep_same_direction_over_threshold():
    b = _benchstat()
    assert b.reproduced([-6.0, -5.0, -9.1], 3)
    assert b.reproduced([5.0, 7.0], 2)
    assert not b.reproduced([-6.0, -4.9, -9.1], 3)  # 하나가 5% 미만
    assert not b.reproduced([-6.0, 6.0], 2)  # 방향이 갈림
    assert not b.reproduced([-6.0, -7.0], 3)  # 짝이 빠진 반복이 있음
    assert not b.reproduced([], 0)


def test_bench_threshold_uses_unrounded_deltas():
    """#26 리뷰 1: 4.96% 차이가 표시용 반올림(5.0)으로 재현 판정되면 안 된다."""
    b = _benchstat()

    def runs(variant_rps):
        return [
            r
            for rep in range(1, 6)
            for r in (_run("0", "baseline", rep, 100.0), _run("0", "mmap_size", rep, variant_rps))
        ]

    def judge(variant_rps):
        s = b.summarize(
            runs(variant_rps),
            cmas=["0"],
            variants=["baseline", "mmap_size"],
            phases=["mixed"],
            metrics=["rps"],
            paired=["rps"],
            reps=5,
        )
        return s["0"]["mixed"]["mmap_size"]["rps"]

    for rps, want in ((104.96, False), (95.04, False), (105.0, True), (95.0, True)):
        deltas = b.paired_deltas(runs(rps), "0", "mmap_size", "mixed", "rps")
        assert b.reproduced(deltas, 5) is want, (rps, deltas)
        entry = judge(rps)
        assert entry["reproduced"] is want, (rps, entry)
    # 표시는 반올림한다(4.96 → 5.0)
    assert judge(104.96)["delta_pct_per_rep"] == [5.0] * 5


def test_bench_metric_reads_nested_keys_and_skips_missing():
    b = _benchstat()
    run = _run("0", "baseline", 1, 10.0, server={"conn_per_db_request": 1.0}, view={"p50_ms": None})
    assert b.metric(run, "mixed", "server.conn_per_db_request") == 1.0
    assert b.metric(run, "mixed", "view.p50_ms") is None
    assert b.metric(run, "write", "rps") is None
    assert b.metric(run, "mixed", "server.missing") is None


def test_bench_summary_marks_only_paired_metrics():
    b = _benchstat()
    runs = [
        _run("0", "baseline", 1, 100.0, p50_ms=2.0, errors=0),
        _run("0", "mmap_size", 1, 94.0, p50_ms=3.0, errors=1),
    ]
    s = b.summarize(
        runs,
        cmas=["0"],
        variants=["baseline", "mmap_size"],
        phases=["mixed"],
        metrics=["rps", "p50_ms"],
        paired=["rps"],
        reps=1,
    )["0"]["mixed"]
    assert s["mmap_size"]["rps"]["delta_pct_per_rep"] == [-6.0]
    assert s["mmap_size"]["rps"]["reproduced"] is True
    assert "reproduced" not in s["baseline"]["rps"]
    assert "delta_pct_per_rep" not in s["mmap_size"]["p50_ms"]
    assert s["mmap_size"]["errors"] == 1


def _loadgen():
    path = ROOT / "lab" / "app" / "lab_tools" / "loadgen.py"
    spec = importlib.util.spec_from_file_location("lab_loadgen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe(t, ckpt_seq, salt1, salt2, **extra):
    return {
        "t": t,
        "cpu_s": t / 2,
        "conn_created": 0,
        "db_requests": 0,
        "db_fds": 3,
        "threads": 2,
        "wal": {"page_size": 4096, "ckpt_seq": ckpt_seq, "salt1": salt1, "salt2": salt2},
        **extra,
    }


def test_loadgen_keeps_wal_headers_and_does_not_call_ckpt_seq_restarts():
    """#26 리뷰 1: ckpt_seq 차이는 재시작 횟수가 아니다. 헤더를 남기고 salt 변화만 하한으로 센다."""
    g = _loadgen()
    first = _probe(0.0, 0, 10, 7)
    # 리뷰 재현과 같은 모양: 두 연결이 번갈아 재시작하면 ckpt_seq 는 0,1,1,2 로,
    # salt1 은 매번 오른다.
    samples = [
        _probe(1.0, 1, 11, 3),
        _probe(2.0, 1, 12, 9),
        _probe(3.0, 2, 13, 4),
        _probe(4.0, 2, 13, 4),
    ]
    out = g.probe_delta(first, samples[-1], samples)
    assert "wal_restarts" not in out
    assert out["wal_ckpt_seq_delta"] == 2
    assert out["wal_salt_changes"] == 3
    assert out["wal_head_start"]["salt1"] == 10
    assert out["wal_head_end"] == samples[-1]["wal"]


def test_loadgen_paired_gaps_are_not_differences_of_medians():
    """#26 리뷰 1: 중앙값의 차는 차의 중앙값이 아니다(client [10,100,101], app [1,99,2])."""
    g = _loadgen()
    client, app = [0.010, 0.100, 0.101], [0.001, 0.099, 0.002]
    paired = g.dist_ms([c - a for c, a in zip(client, app, strict=True)])
    assert paired["p50_ms"] == 9.0
    assert g.dist_ms(client)["p50_ms"] - g.dist_ms(app)["p50_ms"] == 98.0


_TIMED_COUNTER_SCRIPT = r"""
import asyncio, json, sys, tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
tmp = Path(tempfile.mkdtemp())
import django
from django.conf import settings

settings.configure(
    SECRET_KEY="x",
    ROOT_URLCONF=__name__,
    MIDDLEWARE=[],
    ALLOWED_HOSTS=["testserver"],
    DATABASES={
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(tmp / "ok.db")},
        "bad": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(tmp / "missing" / "x.db")},
    },
)
django.setup()
from asgiref.sync import ThreadSensitiveContext
from django.core.handlers.base import BaseHandler
from django.db import connections
from django.db.backends.signals import connection_created
from django.http import Http404, HttpResponse
from django.test import RequestFactory
from django.urls import path
from notes import metrics
from notes.timing import timed

connection_created.connect(metrics.on_connection_created)


def ok(request):
    with connections["default"].cursor() as c:
        c.execute("SELECT 1")
    return HttpResponse("ok")


def sql_error(request):
    with connections["default"].cursor() as c:
        c.execute("SELECT * FROM no_such_table")
    return HttpResponse("unreachable")


def connect_error(request):
    with connections["bad"].cursor() as c:  # 디렉터리가 없어 연결 생성 자체가 실패
        c.execute("SELECT 1")
    return HttpResponse("unreachable")


def not_found(request):
    with connections["default"].cursor() as c:
        c.execute("SELECT 1")
    raise Http404


urlpatterns = [path(n, timed(v)) for n, v in
               (("ok", ok), ("sql", sql_error), ("connect", connect_error), ("404", not_found))]


async def main():
    handler = BaseHandler()
    handler.load_middleware(is_async=True)
    out = {}
    for name in ("ok", "sql", "connect", "404"):
        before = dict(metrics._counts)
        async with ThreadSensitiveContext():
            resp = await handler.get_response_async(RequestFactory().get("/" + name))
        after = dict(metrics._counts)
        out[name] = {"status": resp.status_code,
                     **{k: after[k] - before[k] for k in after}}
    print(json.dumps(out))


asyncio.run(main())
"""


def test_lab_timed_view_counts_failed_db_requests():
    """#26 리뷰 2: 예외로 끝난 DB 요청도 요청당 한 번 센다(연결 생성 비율의 분모)."""
    proc = subprocess.run(
        [sys.executable, "-c", _TIMED_COUNTER_SCRIPT, str(ROOT / "lab" / "app")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    # 정상: 연결 1, 요청 1
    assert out["ok"] == {"status": 200, "conn_created": 1, "db_requests": 1}
    # 연결 뒤 SQL 실패: 연결은 생겼고 요청도 센다(500, 예외는 그대로 전파돼 핸들러가 500 으로 바꿈)
    assert out["sql"] == {"status": 500, "conn_created": 1, "db_requests": 1}
    # 연결 생성 자체 실패: 시그널이 없으므로 연결 0, 요청 1
    assert out["connect"] == {"status": 500, "conn_created": 0, "db_requests": 1}
    # Http404: 쿼리 뒤 404
    assert out["404"] == {"status": 404, "conn_created": 1, "db_requests": 1}


# --- soak(#33) ----------------------------------------------------------------------------


def _soakstat():
    sys.path.insert(0, str(ROOT / "lab"))
    try:
        import _soakstat
    finally:
        sys.path.pop(0)
    return _soakstat


def _soak_row(i, *, n=100, errors=0, fds=None, rss=100_000_000, db_requests=None, kinds=None):
    fds = fds if fds is not None else {"db": 1, "wal": 1, "shm": 1, "socket": 20, "other": 5}
    probe = {
        "fds": {**fds, "total": sum(fds.values()), "scan_ms": 1.0},
        "VmRSS": rss,
        "Threads": 30,
        "py_threads": 5,
        "cgroup": {"current": rss + 10_000_000, "max": 2 * 1024**3, "swap_max": 0},
        "db_requests": db_requests if db_requests is not None else i * n,
        "conn_created": db_requests if db_requests is not None else i * n,
        "wal_bytes": 4_000_000,
        "gc_collections": [i, 0, 0],
    }
    return {
        "i": i,
        "t_s": 10.0 * i,
        "n": n,
        "errors": errors,
        "error_kinds": kinds or ({"http 500": errors} if errors else {}),
        "rps": n / 10,
        "p99_ms": 20.0,
        "cum_requests": i * (n + errors),
        "cum_errors": i * errors,
        "probe": probe,
    }


def test_soak_stop_reason_rss_errors_probe_and_oom():
    g = _loadgen()
    kw = {"mem_frac": 0.8, "err_rate": 0.5, "err_intervals": 3}
    ok = [_soak_row(i) for i in range(1, 6)]
    assert g.stop_reason(ok, **kw) is None
    assert g.stop_reason([], **kw) is None
    # RSS 가 memory.max 의 80% 이상
    big = _soak_row(6, rss=int(0.8 * 2 * 1024**3) + 1)
    assert g.stop_reason([*ok, big], **kw).startswith("rss ")
    # 메모리 한도가 없으면(max) RSS 로 멈추지 않는다
    unlimited = _soak_row(6, rss=10**12)
    unlimited["probe"]["cgroup"]["max"] = None
    assert g.stop_reason([*ok, unlimited], **kw) is None
    # OOM kill 이벤트
    oom = _soak_row(6)
    oom["probe"]["cgroup"]["events_oom_kill"] = 1
    assert g.stop_reason([*ok, oom], **kw) == "oom_kill 1"
    # 오류율: 연속 3구간이어야 멈춘다
    bad = [_soak_row(i, n=10, errors=10) for i in range(6, 8)]
    assert g.stop_reason([*ok, *bad], **kw) is None
    bad.append(_soak_row(8, n=10, errors=10))
    assert g.stop_reason([*ok, *bad], **kw).startswith("error rate")
    # 요청이 하나도 끝나지 않은 구간은 오류율 1
    stuck = [_soak_row(i, n=0) for i in range(6, 9)]
    assert g.stop_reason([*ok, *stuck], **kw).startswith("error rate")
    # 표본 연속 실패
    gone = [{**_soak_row(i), "probe": None} for i in range(6, 9)]
    assert g.stop_reason([*ok, *gone], **{**kw, "err_intervals": 99}).startswith("probe failed")


def test_soak_interval_row_counts_errors_by_kind_and_accumulates():
    g = _loadgen()
    cum: dict = {}
    done = [("read", 0.01, None), ("write", 0.02, "http 500"), ("read", 0.03, "RemoteDisconnected")]
    row = g.interval_row(1, 10.0, 10.0, done, cum, {"x": 1}, None)
    assert (row["n"], row["errors"], row["rps"]) == (1, 2, 0.1)
    assert row["error_kinds"] == {"http 500": 1, "RemoteDisconnected": 1}
    assert row["kinds"] == {"read": 2, "write": 1}
    row2 = g.interval_row(2, 20.0, 10.0, [("read", 0.01, None)], cum, None, "OSError: x")
    assert (row2["cum_requests"], row2["cum_ok"], row2["cum_errors"]) == (4, 2, 2)
    assert row2["probe"] is None and row2["probe_error"] == "OSError: x"


def test_soak_metrics_classify_fds_and_read_proc_status(tmp_path):
    sys.path.insert(0, str(ROOT / "lab" / "app"))
    try:
        from django.conf import settings

        if not settings.configured:
            settings.configure()
        from notes import metrics
    finally:
        sys.path.pop(0)
    db = "/data/app.sqlite3"
    assert metrics.classify_fd(db, db) == "db"
    assert metrics.classify_fd(db + "-wal", db) == "wal"
    assert metrics.classify_fd(db + "-shm", db) == "shm"
    assert metrics.classify_fd("socket:[1234]", db) == "socket"
    assert metrics.classify_fd("/dev/null", db) == "other"
    assert metrics.classify_fd("/data/app.sqlite3-journal", db) == "other"
    fd_dir = tmp_path / "fd"
    fd_dir.mkdir()
    for i, target in enumerate([db, db, db + "-wal", db + "-shm", "socket:[1]", "/dev/null"]):
        (fd_dir / str(i)).symlink_to(target)
    got = metrics.fd_breakdown(db, str(fd_dir))
    assert {k: got[k] for k in ("db", "wal", "shm", "socket", "other", "total")} == {
        "db": 2,
        "wal": 1,
        "shm": 1,
        "socket": 1,
        "other": 1,
        "total": 6,
    }
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmHWM:\t  2048 kB\nVmRSS:\t  1024 kB\nThreads:\t7\n")
    assert metrics.proc_status(str(status)) == {"VmHWM": 2097152, "VmRSS": 1048576, "Threads": 7}


def test_soak_ols_and_shapes():
    s = _soakstat()
    fit = s.ols([0, 1, 2, 3], [1, 3, 5, 7])
    assert fit["slope"] == 2 and fit["intercept"] == 1 and fit["r2"] == 1
    assert s.ols([1, 1, 1], [1, 2, 3]) is None
    assert s.ols([1, 2], [1, 2]) is None

    def pts(ys):
        return [{"t_s": 10 * i, "x": 1000 * i, "y": y} for i, y in enumerate(ys)]

    assert s.trend(pts([30] * 10), "x", "y")["shape"] == "flat"
    grows = s.trend(pts([30 + 3 * i for i in range(10)]), "x", "y")
    assert grows["shape"] == "grows" and grows["max"] == 57 and grows["t_max_s"] == 90
    plateau = s.trend(pts([30, 300, 600, 900, 1200, 1210, 1210, 1210, 1210, 1210]), "x", "y")
    assert plateau["shape"] == "plateau"


def test_soak_analyze_none_like_growth_and_extrapolation():
    """None 모양: DB 본체 fd 가 요청마다 쌓이고 RSS 가 같이 는다 → 연결당 메모리·한도 도달 추정."""
    s = _soakstat()
    rows = []
    for i in range(1, 31):
        conns = 10 * i  # 구간마다 연결 10개 누적
        fds = {"db": conns, "wal": conns, "shm": 1, "socket": 20, "other": 5}
        rows.append(_soak_row(i, fds=fds, rss=50_000_000 + 200_000 * conns))
    out = s.analyze(rows, fd_limit=1_048_576, mem_limit=2 * 1024**3)
    assert out["fd_dbfiles_vs_db_requests"]["shape"] == "grows"
    assert out["rss_vs_db_requests"]["shape"] == "grows"
    assert out["rss_per_connection_est"]["bytes"] == 200_000
    assert out["rss_per_connection_est"]["r2"] == 1.0
    assert out["fd_per_s_last_half"] == 2.0  # 10초에 fd 20
    # 2 GiB 까지: (2147483648 - 110000000) / 200000 B/s
    assert round(out["eta_mem_limit_s_est"]) == round((2 * 1024**3 - 110_000_000) / 200_000)
    assert out["fd_end"]["fd_dbfiles"] == 601
    assert out["conn_per_db_request"] == 1.0


def test_soak_analyze_flat_run_has_no_per_connection_estimate_or_eta():
    s = _soakstat()
    rows = [_soak_row(i) for i in range(1, 31)]
    rows[3] = {**rows[3], "probe": None}  # 표본 실패 구간은 뺀다
    out = s.analyze(rows, fd_limit=1_048_576, mem_limit=2 * 1024**3)
    assert out["intervals_with_probe"] == 29
    assert out["fd_dbfiles_vs_db_requests"]["shape"] == "flat"
    assert out["rss_per_connection_est"] is None
    assert out["eta_fd_limit_s_est"] is None and out["eta_mem_limit_s_est"] is None
    assert s.time_to(100, 50, 0) is None and s.time_to(100, 50, 5) == 10


def test_soak_nofile_limits_parses_proc_limits():
    s = _soakstat()
    text = (
        "Limit                     Soft Limit           Hard Limit           Units     \n"
        "Max processes             unlimited            unlimited            processes \n"
        "Max open files            1048576              1048576              files     \n"
    )
    assert s.nofile_limits(text) == (1048576, 1048576)


# --- soak 리뷰 1(#33): 종료 꼬리 집계·발생기 실패·출력 없는 매달림 ---------------------------


class _LateConn:
    """요청마다 0.2초 뒤 HTTP 500. ``/lab/soakprobe`` 는 바로 표본을 준다."""

    made = 0
    lock = __import__("threading").Lock()

    def __init__(self, *a, **kw):
        self.path = None

    def request(self, method, path, **kw):
        self.path = path

    def getresponse(self):
        from types import SimpleNamespace

        if self.path == "/lab/soakprobe":
            body = json.dumps({"VmRSS": 1, "cgroup": {"max": 10**12}}).encode()
            return SimpleNamespace(status=200, read=lambda: body)
        import time as _t

        _t.sleep(0.2)
        with _LateConn.lock:
            _LateConn.made += 1
        return SimpleNamespace(status=500, read=lambda: b"late failure")

    def close(self):
        pass


def test_soak_loadgen_counts_requests_finishing_after_the_last_interval(monkeypatch, capsys):
    """리뷰 1 P2: 마지막 구간을 뗀 뒤 끝난 요청(성공·실패)도 최종 집계에 들어가야 한다."""
    from types import SimpleNamespace

    g = _loadgen()
    _LateConn.made = 0
    monkeypatch.setattr(g.http.client, "HTTPConnection", _LateConn)
    args = SimpleNamespace(
        duration=0.1,
        report_every=0.1,
        concurrency=2,
        read=1.0,
        sorted=0.0,
        max_id=1,
        write_rows=1,
        seed=1,
        stop_mem_frac=0.8,
        stop_err_rate=0.5,
        stop_err_intervals=3,
    )
    g.soak(args, SimpleNamespace(hostname="unused", port=80))
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    final = lines[-1]
    rows = lines[:-1]
    assert final["final"] is True
    assert _LateConn.made == 2  # 워커 둘이 하나씩 보내고 끝(0.2초 > 0.1초)
    assert final["cum_requests"] == _LateConn.made
    assert final["cum_errors"] == _LateConn.made
    assert sum(sum(r["error_kinds"].values()) for r in rows) == _LateConn.made
    assert rows[-1]["cum_requests"] == _LateConn.made


def _soak_module():
    sys.path.insert(0, str(ROOT / "lab"))
    try:
        spec = importlib.util.spec_from_file_location(
            "lab_test_soak", ROOT / "lab" / "test_soak.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class _FakeStack:
    def down(self):
        pass

    def up_infra(self):
        pass

    def up_app(self, *a, **kw):
        return 1

    def compose(self, *a, **kw):
        pass

    def argv(self):
        return ["unused"]

    def state(self, *a):
        return {"Status": "running", "ExitCode": 0, "OOMKilled": False}

    def stop_app(self, *a):
        pass

    def logs(self, *a):
        return ""


class _BlockingPipe:
    """``kill()`` 될 때까지(최대 ``hold`` 초 뒤 스스로 끝남) 아무것도 내지 않다가 EOF."""

    def __init__(self, proc, text=""):
        self.proc, self.text = proc, text

    def __iter__(self):
        self.proc.wait()
        yield from self.text.splitlines(keepends=True)

    def read(self):
        self.proc.wait()
        return self.text


class _FakeProc:
    def __init__(self, *, rc, hold=0.0, out="", err=""):
        import threading

        self.killed = threading.Event()
        self.hold = hold
        self.stdout = _BlockingPipe(self, out)
        self.stderr = _BlockingPipe(self, err)
        self.rc = rc
        self.kill_calls = 0

    def kill(self):
        self.kill_calls += 1
        self.killed.set()

    def wait(self, timeout=None):
        limit = self.hold if timeout is None else min(timeout, self.hold)
        self.killed.wait(limit)
        return -9 if self.kill_calls else self.rc

    def poll(self):
        return None if not self.killed.is_set() and self.hold else self.wait(0)


def _patch_soak(monkeypatch, tmp_path, proc):
    from types import SimpleNamespace

    m = _soak_module()
    monkeypatch.setattr(m, "OUT", tmp_path)
    monkeypatch.setattr(m, "CMAS", ("0",))
    monkeypatch.setattr(m.Volumes, "take", classmethod(lambda cls: "v01"))
    limits = "Max open files            1048576              1048576              files     \n"
    env = {
        "conn_max_age": 0,
        "self_limits": limits,
        "pid1_limits": limits,
        "nr_open": "1048576\n",
        "cgroup": {"max": 2 * 1024**3, "swap_max": 0},
    }
    probe = _soak_row(0)["probe"]
    monkeypatch.setattr(
        m, "http_json", lambda method, port, path, **kw: env if path == "/lab/soakenv" else probe
    )
    import time as real_time

    monkeypatch.setattr(
        m, "time", SimpleNamespace(sleep=lambda s: None, monotonic=real_time.monotonic)
    )
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **kw: proc)
    return m


def test_soak_harness_fails_when_loadgen_fails(monkeypatch, tmp_path):
    """리뷰 1 P2: 발생기 rc 1·출력 없음이 정상 soak 로 통과하면 안 된다(CONN_MAX_AGE 무관)."""
    proc = _FakeProc(rc=1, err="loadgen failed\n")
    m = _patch_soak(monkeypatch, tmp_path, proc)
    with pytest.raises(AssertionError, match="loadgen rc 1"):
        m.test_soak(_FakeStack(), "rid", {})
    summary = json.loads(next(tmp_path.glob("soak-*-summary.json")).read_text())
    problems = summary["by_cma"]["0"]["harness_problems"]
    assert "loadgen rc 1" in problems and "no final line" in problems and "no intervals" in problems


def test_soak_harness_times_out_without_output(monkeypatch, tmp_path):
    """리뷰 1 P2: 발생기가 아무것도 내지 않고 매달려도 기한에 kill 하고 사유를 남긴다."""
    import time

    proc = _FakeProc(rc=0, hold=5.0)
    m = _patch_soak(monkeypatch, tmp_path, proc)
    monkeypatch.setattr(m, "DURATION", 0.2)
    monkeypatch.setattr(m, "HARNESS_GRACE", 0.3, raising=False)
    t0 = time.monotonic()
    with pytest.raises(AssertionError, match="harness timeout"):
        m.test_soak(_FakeStack(), "rid", {})
    assert proc.kill_calls >= 1
    assert time.monotonic() - t0 < 3.0  # 5초 매달림을 기다리지 않았다
    raw = [json.loads(x) for x in next(tmp_path.glob("soak-*[0-9].jsonl")).read_text().splitlines()]
    summary = next(r for r in raw if r["kind"] == "summary")
    assert summary["stop_reason"] == "harness timeout"
    assert "harness timeout" in summary["harness_problems"]


def test_soak_quantiles_are_defined():
    """리뷰 1 P3: 중앙값은 짝수 개면 가운데 둘의 평균, p90 은 최근접 순위(보간 없음)."""
    s = _soakstat()
    assert s.describe([1, 2, 366, 369])["median"] == 184.0
    values = list(range(1, 181))  # 180 개
    assert s.quantile_nearest(values, 0.9) == 162  # ceil(0.9·180) = 162 번째
    assert s.quantile_nearest([5], 0.9) == 5 and s.quantile_nearest([], 0.9) is None


def test_soak_harness_problems_allow_safe_stop_but_not_loadgen_failure():
    s = _soakstat()
    kw = {"duration": 30, "interval": 10}
    rows = [_soak_row(i) for i in range(1, 4)] + [{**_soak_row(4), "tail": True}]
    final = {"final": True, "stop_reason": None, "workers_alive": 0}
    assert s.harness_problems(rows, final, 0, **kw) == []
    # 의도된 안전 정지로 짧게 끝난 것은 실패가 아니다
    safe = {**final, "stop_reason": "rss ..."}
    assert s.harness_problems(rows[:1], safe, 0, **kw) == []
    assert s.harness_problems(rows[:2], final, 0, **kw) == ["short run: 2 < 3 intervals"]
    assert s.harness_problems([], None, 1, **kw) == [
        "loadgen rc 1",
        "no final line",
        "no intervals",
    ]
    assert "harness timeout" in s.harness_problems(rows, final, -9, timed_out=True, **kw)
    assert s.harness_problems(rows, {**final, "workers_alive": 2}, 0, **kw) == ["workers alive 2"]
    no_probe = [{**r, "probe": None} for r in rows]
    assert s.harness_problems(no_probe, final, 0, **kw) == ["no probe samples"]


def test_soak_analyze_counts_tail_but_keeps_it_out_of_interval_metrics():
    s = _soakstat()
    rows = [_soak_row(i) for i in range(1, 41)]
    tail = _soak_row(41, n=1, errors=1)
    tail.update({"tail": True, "rps": 9999.0, "p99_ms": 9999.0, "cum_requests": 40 * 100 + 2})
    tail["cum_errors"] = 1
    baseline = {"db_requests": 0}
    tail["probe"]["db_requests"] = 4002
    out = s.analyze([*rows, tail], fd_limit=None, mem_limit=None, baseline=baseline)
    assert out["intervals"] == 40 and out["intervals_with_probe"] == 40
    assert out["requests"] == 4002 and out["errors"] == 1
    assert (out["tail_requests"], out["tail_errors"]) == (2, 1)
    assert out["p99_ms_max"] == 20.0 and out["rps_mean"] == 10.0
    assert out["error_kinds"] == {"http 500": 1}
    assert out["server_minus_client_requests"] == 0
    assert out["dist"]["fd_total"]["median"] == 28 and out["steady_mean"]["fd_total"] == 28


def test_soak_report_builds_tables_from_raw_jsonl(tmp_path):
    """리뷰 1 P3: 결과 문서의 표는 원자료에서 스크립트로 만든다(손으로 옮기지 않는다)."""
    limits = "Max open files            1048576              1048576              files     \n"
    lines = []
    for cma, fds_db in (("none", 800), ("0", 32)):
        env = {
            "pid": 36,
            "self_limits": limits,
            "pid1_limits": limits,
            "nr_open": "1048576\n",
            "file_max": "9\n",
            "cgroup": {"max": 2 * 1024**3, "swap_max": 0},
            "conn_max_age": None if cma == "none" else 0,
            "pid1_cmdline": "litestream replicate",
        }
        lines.append({"cma": cma, "kind": "env", **env})
        lines.append({"cma": cma, "kind": "baseline", "probe": {**_soak_row(0)["probe"]}})
        fds = {"db": fds_db, "wal": 10, "shm": 1, "socket": 20, "other": 5}
        for i in range(1, 41):
            row = _soak_row(i, fds=fds, rss=(400 if cma == "none" else 70) * 2**20)
            lines.append({"cma": cma, "kind": "interval", **row})
        lines.append({"cma": cma, "kind": "final", "final": True, "stop_reason": None})
    raw = tmp_path / "soak-x.jsonl"
    raw.write_text("".join(json.dumps(x) + "\n" for x in lines))
    proc = subprocess.run(
        [sys.executable, str(ROOT / "lab" / "soak_report.py"), str(raw)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "| 300 | 3,000 | 10.0 | 20.0 | 0 | 836 | 800 | 10 | 1 | 20 | 400 | 30 | 410 |" in out
    assert (
        "| fd 합계 최소 / 중앙값 / p90 / 최대 | 836 / 836 / 836 / 836 | 68 / 68 / 68 / 68 |" in out
    )
    assert "RSS 비 5.71" in out


# --- cache_size 최소 재현 집계(#27, lab/_cachestat.py·cache_probe.py·cache_report.py) ------------


def _lab_module(name):
    spec = importlib.util.spec_from_file_location(f"lab_{name}", ROOT / "lab" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "lab"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(ROOT / "lab"))
    return module


def _cell(variant, block, ops, p99=100.0, size="small", threads=1, mode="perconn", kind="cell"):
    return {
        "kind": kind,
        "size": size,
        "threads": threads,
        "mode": mode,
        "block": block,
        "pos": 0,
        "variant": variant,
        "ops_per_s": ops,
        "p99_us": p99,
        "phases_us": {"open": {"p50": 10.0}},
        "errors": {},
    }


def test_cache_pairs_within_block_and_condition_and_skips_warmup():
    s = _lab_module("_cachestat")
    cond = ("small", 1, "perconn")
    cells = s.cells_of(
        [
            _cell("baseline", 0, 100.0, kind="warmup"),
            _cell("baseline", 1, 200.0),
            _cell("baseline", 0, 100.0),
            _cell("cache_size", 1, 180.0),
            _cell("cache_size", 0, 90.0),
            _cell("baseline", 0, 50.0, mode="reuse"),
            _cell("cache_size", 0, 60.0, mode="reuse"),
            _cell("cache_size", 2, 70.0),  # 짝이 없는 블록
        ]
    )
    assert s.paired_deltas(cells, cond, "cache_size", "ops_per_s") == [-10.0, -10.0]
    assert s.paired_deltas(cells, ("small", 1, "reuse"), "cache_size", "ops_per_s") == [20.0]
    assert s.value(cells[0], "phases_us.open.p50") == 10.0
    assert s.value(cells[0], "phases_us.close.p50") is None


def test_cache_mean_ci_uses_t_distribution():
    s = _lab_module("_cachestat")
    m, lo, hi = s.mean_ci([1.0, 2.0, 3.0])  # 평균 2, 표준편차 1, n=3, t(2)=4.303
    assert m == 2.0
    assert abs(hi - (2.0 + 4.303 / 3**0.5)) < 1e-9
    assert abs(lo - (2.0 - 4.303 / 3**0.5)) < 1e-9
    assert s.mean_ci([1.0]) is None
    assert s.t975(11) == 2.201
    assert s.t975(100) == 1.96


def test_cache_verdict_needs_ci_away_from_zero_and_five_percent():
    s = _lab_module("_cachestat")
    assert s.verdict([-8.0, -9.0, -10.0, -9.0]) == s.REPRODUCED_WORSE
    assert s.verdict([8.0, 9.0, 10.0, 9.0]) == s.REPRODUCED_BETTER
    # 지연은 작을수록 좋다
    assert s.verdict([8.0, 9.0, 10.0, 9.0], higher_is_better=False) == s.REPRODUCED_WORSE
    # 구간이 0 을 벗어나도 평균이 5% 미만이면 재현이 아니다(구간이 ±5% 안이면 효과 없음)
    assert s.verdict([-2.0, -2.1, -1.9, -2.0]) == s.NO_EFFECT
    assert s.verdict([0.5, -0.5, 0.2, -0.2]) == s.NO_EFFECT
    # 흔들림이 커서 구간이 넓으면 판정 불가
    assert s.verdict([-15.0, 19.0, -10.0, -9.0]) == s.UNDECIDED
    # 평균은 5% 이상이지만 구간이 0 을 포함
    assert s.verdict([-30.0, 10.0, -12.0]) == s.UNDECIDED
    assert s.verdict([-9.0]) == s.UNDECIDED


def test_cache_same_direction_is_the_bench_rule():
    s = _lab_module("_cachestat")
    assert s.same_direction([-6.0, -5.0], 2)
    assert not s.same_direction([-6.0, -4.9], 2)
    assert not s.same_direction([-6.0, 6.0], 2)
    assert not s.same_direction([-6.0], 2)


def test_cache_probe_pragmas_match_recommended_table():
    from django_sqlite_ops.database import recommended

    p = _lab_module("cache_probe")
    assert p.RECOMMENDED_PRAGMAS == recommended()["pragmas"]
    stmts = p.pragma_statements("cache_size")
    assert stmts[:2] == list(p.DJANGO_PRAGMAS)
    assert stmts[-1] == "PRAGMA cache_size=-65536"
    assert p.pragma_statements("baseline") == [
        *p.DJANGO_PRAGMAS,
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA busy_timeout=5000",
    ]
    assert p.quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert p.quantile([1.0, 2.0, 3.0, 4.0], 0.99) == 4.0
    assert p.quantile([], 0.5) is None
    # 블록 순서는 블록 번호로 고정되고 블록마다 다르다
    order = [p.block_order(b, list(p.VARIANTS)) for b in range(6)]
    assert order[0] == p.block_order(0, list(p.VARIANTS))
    assert len({tuple(o) for o in order}) > 1


def test_cache_report_renders_from_raw_records(tmp_path, capsys):
    r = _lab_module("cache_report")
    meta = {
        "kind": "meta",
        "env": "linux",
        "python": "3.13",
        "sqlite": "3.46.1",
        "platform": "Linux",
        "cpus": 4,
        "cgroup_memory_max": "max",
        "sizes": {"small": {"rows": 10, "bytes": 2**20, "page_size": 4096, "pages": 256}},
        "blocks": 2,
        "cell_s": 1.0,
        "probe_sha256": "0" * 64,
        "sqlite_status": False,
        "variants": ["baseline", "cache_size"],
        "conditions": [["small", 1, "perconn"]],
    }
    cells = [
        _cell("baseline", 0, 100.0),
        _cell("cache_size", 0, 90.0),
        _cell("baseline", 1, 100.0),
        _cell("cache_size", 1, 89.0),
    ]
    path = tmp_path / "cache.jsonl"
    lines = [meta, *cells]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    with pytest.raises(SystemExit, match="end"):
        r.load(path)
    path.write_text("\n".join(json.dumps(x) for x in [*lines, {"kind": "end"}]) + "\n")
    assert r.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "| small | 1 | perconn | `cache_size` | 100 | -10.5 [" in out
    assert "| -11.0 … -10.0 |" in out
    assert "SQLite 메모리" not in out  # sqlite3_status 가 없으면 RSS 만


def test_cache_bench_kind_rows_reads_both_bench_formats():
    s = _lab_module("_cachestat")

    def kinds(read, sorted_, write, svc=False):
        def k(ms):
            d = {"mean_ms": ms + 100}  # 클라이언트 값은 svc 와 다르게 둔다
            if svc:
                d["svc"] = {"mean_ms": ms}
            return d

        return {"read": k(read), "sorted": k(sorted_), "write": k(write)}

    # #10 형식: 실행에 cma 가 없고 kinds 가 바로 있다. 클라이언트 평균을 쓴다.
    v10 = {
        "runs": [
            {"variant": "baseline", "rep": 1, "kinds": kinds(0.0, 25.0, 0.0)},
            {"variant": "cache_size", "rep": 1, "kinds": kinds(10.0, 43.0, 0.0)},
        ]
    }
    (row,) = s.bench_kind_rows(v10, ["cache_size"])
    assert row["source"] == "client"
    assert row["d_read"] == 10.0 and row["base_ratio"] == 1.25
    assert abs(row["ratio"] - 143 / 110) < 1e-12
    # #26 형식: phases.mixed.kinds 에 svc 가 있고 cma 로 거른다
    v26 = {
        "runs": [
            {"cma": "0", "variant": "baseline", "rep": 1,
             "phases": {"mixed": {"kinds": kinds(10.0, 15.0, 10.0, svc=True)}}},
            {"cma": "0", "variant": "cache_size", "rep": 1,
             "phases": {"mixed": {"kinds": kinds(11.0, 22.0, 10.0, svc=True)}}},
            {"cma": "none", "variant": "cache_size", "rep": 1,
             "phases": {"mixed": {"kinds": kinds(1.0, 1.0, 1.0, svc=True)}}},
        ]
    }  # fmt: skip
    (row,) = s.bench_kind_rows(v26, ["cache_size"])
    assert row["source"] == "svc"
    assert abs(row["d_sorted"] - 100 * 7 / 15) < 1e-12
    assert row["ratio"] == 2.0 and row["base_ratio"] == 1.5
    assert s.bench_kind_rows(v26, ["mmap_size"]) == []


# --- 워커 실패 전파(#27 리뷰 1) ----------------------------------------------------------------


def _call_with_deadline(fn, timeout=20.0):
    """``fn()`` 을 다른 스레드에서 부르고 (끝났는가, 반환값, 예외)를 돌려준다."""
    import threading

    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — 테스트가 예외 자체를 본다
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive(), box.get("value"), box.get("error")


@pytest.fixture
def small_db(tmp_path):
    p = _lab_module("cache_probe")
    path = tmp_path / "db" / "db.sqlite3"
    path.parent.mkdir()
    p.seed(path, 500)
    return path


@pytest.mark.parametrize("threads", [1, 2])
def test_cache_mech_worker_failure_fails_the_cell(small_db, monkeypatch, threads):
    import sqlite3

    m = _lab_module("cache_mech")
    calls = {"n": 0}

    def flaky(conn, kind, rng, max_id, lo=None):
        calls["n"] += 1
        if calls["n"] > 3:  # 몇 번은 성공하고 그 뒤 실패(일부 워커만 실패하는 경우 포함)
            raise sqlite3.OperationalError("injected worker failure")

    monkeypatch.setattr(m, "one", flaky)
    done, value, error = _call_with_deadline(
        lambda: m.run_cell(small_db, "baseline", "perconn", "read", threads, 0.3, 1)
    )
    assert done, "run_cell hung"
    assert value is None, f"failed worker was reported as a normal cell: {value}"
    assert isinstance(error, RuntimeError) and "injected worker failure" in str(error)


def test_cache_mech_reuse_connect_failure_does_not_hang(small_db, monkeypatch):
    m = _lab_module("cache_mech")

    def broken(path, config):
        raise OSError("injected connect failure")

    monkeypatch.setattr(m, "open_conn", broken)
    done, value, error = _call_with_deadline(
        lambda: m.run_cell(small_db, "baseline", "reuse", "read", 2, 0.3, 1)
    )
    assert done, "run_cell hung on a worker that failed before the barrier"
    assert value is None
    assert isinstance(error, RuntimeError) and "injected connect failure" in str(error)


@pytest.mark.parametrize("threads", [1, 2])
def test_cache_probe_unexpected_worker_failure_fails_the_cell(small_db, monkeypatch, threads):
    p = _lab_module("cache_probe")
    calls = {"n": 0}
    real = p.request

    def flaky(conn, rng, max_id):
        calls["n"] += 1
        if calls["n"] > 3:
            raise ValueError("injected worker failure")
        return real(conn, rng, max_id)

    monkeypatch.setattr(p, "request", flaky)
    done, value, error = _call_with_deadline(
        lambda: p.run_cell(small_db, "baseline", "perconn", threads, 0.3, 1)
    )
    assert done, "run_cell hung"
    assert value is None, f"failed worker was reported as a normal cell: {value}"
    assert isinstance(error, RuntimeError) and "injected worker failure" in str(error)


def test_cache_probe_reuse_connect_failure_does_not_hang(small_db, monkeypatch):
    p = _lab_module("cache_probe")

    def broken(path, variant):
        raise OSError("injected connect failure")

    monkeypatch.setattr(p, "connect", broken)
    done, value, error = _call_with_deadline(
        lambda: p.run_cell(small_db, "baseline", "reuse", 2, 0.3, 1)
    )
    assert done, "run_cell hung on a worker that failed before the barrier"
    assert value is None
    assert isinstance(error, RuntimeError) and "injected connect failure" in str(error)


def test_cache_probe_sqlite_errors_are_counted_not_lost(small_db, monkeypatch):
    """SQLite 오류(예: busy)는 칸을 멈추지 않고 errors 에 센다. 보고서가 오류 수를 낸다."""
    import sqlite3

    p = _lab_module("cache_probe")

    def busy(conn, rng, max_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(p, "request", busy)
    r = p.run_cell(small_db, "baseline", "perconn", 1, 0.2, 1)
    assert r["ops"] == 0
    assert sum(r["errors"].values()) > 0


def test_cache_probe_perconn10_counts_queries_not_connections(small_db):
    p = _lab_module("cache_probe")
    r = p.run_cell(small_db, "baseline", "perconn10", 1, 0.2, 1)
    assert r["ops"] > 0 and r["ops"] % 10 == 0
    assert sum(r["kinds"].values()) == r["ops"]
    assert set(r["phases_us"]) == {"open", "pragma", "query", "close"}


def test_cache_mech_multi_query_conditions_count_queries(small_db):
    m = _lab_module("cache_mech")
    for kind in ("sort10same", "sort10diff"):
        r = m.run_cell(small_db, "baseline", "perconn", kind, 1, 0.2, 1)
        assert r["ops"] > 0 and r["ops"] % 10 == 0, (kind, r)
        assert r["close_mean_us"] is not None


# --- Django 기본 대 권장 설정 비교(#83, lab/dbconfig_report.py) ---------------------------------


def test_dbconfig_locked_verdict_needs_every_rep():
    r = _lab_module("dbconfig_report")
    assert r.locked_verdict([3, 1, 9], [0, 0, 0], 3) == "재현"
    assert r.locked_verdict([3, 0, 9], [0, 0, 0], 3) == "재현 안 됨"
    assert r.locked_verdict([3, 1, 9], [0, 1, 0], 3) == "재현 안 됨"
    assert r.locked_verdict([3, 1], [0, 0], 3) == "판정 불가"


def _dbconfig_run(config, workers, rep, rps, locked):
    return {
        "cma": f"w{workers}",
        "variant": "baseline" if config == "django-default" else "recommended",
        "config": config,
        "workers": workers,
        "rep": rep,
        "pragmas": {"journal_mode": "delete" if config == "django-default" else "wal"},
        "transaction_mode": None if config == "django-default" else "IMMEDIATE",
        "phases": {
            "rmw": {
                "rps": rps,
                "p50_ms": 1.0,
                "p99_ms": 10.0,
                "errors": locked,
                "locked": locked,
                "kinds": {"rmw": {"p99_ms": 9.0, "errors": locked}},
                "exceptions": {"x.DoesNotExist: y": 2} if locked else {},
            }
        },
    }


def test_dbconfig_report_csv_and_tables():
    r = _lab_module("dbconfig_report")
    runs = [
        _dbconfig_run(c, 1, rep, rps, locked)
        for rep in (1, 2)
        for c, rps, locked in (("django-default", 100.0, 5), ("recommended", 150.0, 0))
    ]
    data = {
        "reps": 2,
        "duration_s": 20,
        "concurrency": 16,
        "rows": 1000,
        "workers": [1],
        "configs": ["django-default", "recommended"],
        "phases": {"rmw": {}},
        "runs": runs,
    }
    rows = r.csv_rows(data)
    assert [(x["rep"], x["config"], x["locked"]) for x in rows] == [
        (1, "django-default", 5),
        (1, "recommended", 0),
        (2, "django-default", 5),
        (2, "recommended", 0),
    ]
    assert rows[0]["transaction_mode"] == "" and rows[1]["transaction_mode"] == "IMMEDIATE"
    assert set(rows[0]) == set(r.CSV_FIELDS)
    out = r.tables(data)
    assert (
        "| `rmw` | +50.0, +50.0 | +0.0, +0.0 | 예 | 아니오 | 재현 (기본 [5, 5], 권장 [0, 0]) |"
        in out
    )
    # locked 밖의 예외도 숨기지 않는다
    assert "x.DoesNotExist: y': 4" in out
