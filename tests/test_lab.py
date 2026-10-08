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
