"""``import django`` 를 막은 서브프로세스에서 코드를 돌리는 테스트 헬퍼."""

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_PRELUDE = """
import sys

class BlockDjango:
    def find_spec(self, name, path=None, target=None):
        if name == "django" or name.startswith("django."):
            raise ImportError(f"blocked: {name}")
        return None

sys.meta_path.insert(0, BlockDjango())
"""

_EPILOGUE = """
leaked = sorted(m for m in sys.modules if m == "django" or m.startswith("django."))
assert not leaked, leaked
print("ok")
"""


def assert_runs_without_django(body: str) -> None:
    """``body`` 를 Django import 가 막힌 새 인터프리터에서 실행하고 Django 가 새지 않았는지 본다."""
    code = _PRELUDE + textwrap.dedent(body) + _EPILOGUE
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
