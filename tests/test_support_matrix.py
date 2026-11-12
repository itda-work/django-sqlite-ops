"""PyPI 에 보이는 지원 범위(classifier)와 CI ``test`` 매트릭스가 같은 집합인지 본다 (#101).

classifier 만 고치고 CI 매트릭스를 빠뜨리면(또는 그 반대면) 돌리지 않은 판을 지원한다고 말하게 된다.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _classifier_versions(prefix: str) -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pattern = re.compile(rf"^{re.escape(prefix)} :: (\d+\.\d+)$")
    return {m[1] for c in project["classifiers"] if (m := pattern.match(c))}


def _job_block(name: str) -> str:
    """``jobs:`` 아래 두 칸 들여쓴 잡 하나의 본문."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    starts = [(m.group(1), m.start()) for m in re.finditer(r"(?m)^  ([\w-]+):\s*$", text)]
    for i, (job, start) in enumerate(starts):
        if job == name:
            end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
            return text[start:end]
    raise AssertionError(f"job {name!r} not found in {CI_WORKFLOW}")


def _matrix_list(block: str, key: str) -> set[str]:
    (line,) = re.findall(rf"(?m)^\s+{key}: \[(.*)\]\s*$", block)
    return set(re.findall(r'"([^"]+)"', line))


def test_python_classifiers_match_ci_test_matrix():
    assert _classifier_versions("Programming Language :: Python") == _matrix_list(
        _job_block("test"), "python"
    )


def test_django_classifiers_match_ci_test_matrix():
    assert _classifier_versions("Framework :: Django") == _matrix_list(_job_block("test"), "django")
