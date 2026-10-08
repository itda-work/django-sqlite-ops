"""회귀 랩(lab/, #10)의 Docker 없이 확인할 수 있는 약속.

- 랩 시나리오는 ``RUN_LAB=1`` 일 때만 수집된다. 기본 ``pytest`` 는 Docker 가 필요 없다.
- 랩 빌드 컨텍스트는 배포 프로필 문서의 조각에서 만든다. 문서와 다른 줄은 ``SUBSTITUTIONS`` 뿐이다.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

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


def _real_health_timeline(observations: list[float], outage: float = 20):
    """실제 ``health.alias_status`` 로 만든 단절 중 표본(1초 간격, 0.1초부터).

    ``observations`` 는 성공한 조회가 끝난 시각(단절 시작 = 0초)이다. 각 표본 시각에는 그때까지
    끝난 마지막 관측이 보인다.
    """
    from django_sqlite_ops.boot.decide import RemoteTxid
    from django_sqlite_ops.health import Sample, alias_status
    from django_sqlite_ops.wal import IN_SYNC, WalEvidence

    timeline = []
    t = 0.1
    while t < outage:
        observed = max(o for o in observations if o <= t)
        sample = Sample(
            observed=observed,
            checked_at=0,
            local=1,
            remote=RemoteTxid(1),
            wal=WalEvidence(IN_SYNC, "in sync"),
        )
        r = alias_status(sample, t, refresh=2, grace=10)
        timeline.append((round(t, 1), r["status"], r["code"], r["age"]))
        t += 1.0
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
