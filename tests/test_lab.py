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


# --- PRAGMA 벤치 집계(#26, lab/_benchstat.py) ------------------------------------------------


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
