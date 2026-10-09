"""회귀 랩 pytest 설정. ``RUN_LAB=1`` 일 때만 수집한다(기본 ``pytest`` 는 Docker 가 필요 없다).

결과(통과/실패, 소요 시간, 시나리오가 남긴 핵심 값)는 ``lab/.out/results-<run>.jsonl`` 에 쌓인다.
"""

import json
import os
import sys
from pathlib import Path

import pytest

LAB_DIR = Path(__file__).resolve().parent

if os.environ.get("RUN_LAB") != "1":
    collect_ignore_glob = ["test_*.py"]

sys.path.insert(0, str(LAB_DIR))

from _lab import OUT, RUN_ID  # noqa: E402 — 시나리오와 같은 값(_lab 이 정본)

RESULTS = OUT / f"results-{RUN_ID}.jsonl"


def pytest_configure(config):
    config.addinivalue_line("markers", "bench: PRAGMA 벤치(scripts/lab.sh bench)")
    config.addinivalue_line("markers", "soak: fd·RSS 장시간 실행(scripts/lab.sh soak, #33)")
    config.addinivalue_line("markers", "profile: 배포 프로필 문서 compose 종단 검증")


@pytest.fixture(scope="session")
def run_id() -> str:
    return RUN_ID.replace("-", "")[-10:]


@pytest.fixture(scope="session")
def stack():
    from _lab import Stack

    s = Stack("main")
    s.up_infra()
    return s


@pytest.fixture
def rec(request):
    """시나리오가 결과 문서에 남길 값을 담는다."""
    notes: dict = {}
    request.node._lab_notes = notes
    return notes


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" and not (report.when == "setup" and report.failed):
        return
    OUT.mkdir(parents=True, exist_ok=True)
    row = {
        "test": item.name,
        "outcome": report.outcome,
        "duration_s": round(report.duration, 1),
        "notes": getattr(item, "_lab_notes", {}),
    }
    if report.failed:
        row["error"] = str(report.longrepr)[-2000:]
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
