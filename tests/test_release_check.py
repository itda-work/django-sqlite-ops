"""scripts/release_check.py 가 태그·버전·CHANGELOG·분류의 어긋남을 거부하는지 확인한다 (#77)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts/release_check.py")
release_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_check)

PYPROJECT = """\
[project]
name = "example"
version = "{version}"
classifiers = [{classifiers}]
"""
ALPHA = '"Development Status :: 3 - Alpha"'
CHANGELOG = """\
# Changelog

## [Unreleased]

## [{version}] - 2026-10-26

### Added

- 무언가

[Unreleased]: https://example.invalid/compare/v{version}...HEAD
[{version}]: https://example.invalid/releases/tag/v{version}
"""


def _project(tmp_path, version="0.1.0a1", classifiers=ALPHA, changelog=None):
    (tmp_path / "pyproject.toml").write_text(
        PYPROJECT.format(version=version, classifiers=classifiers), encoding="utf-8"
    )
    if changelog is not False:
        text = CHANGELOG.format(version=version) if changelog is None else changelog
        (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_consistent_prerelease_passes(tmp_path, capsys):
    root = _project(tmp_path)
    out = tmp_path / "gh_output"
    assert release_check.main(["v0.1.0a1", "--github-output", str(out)], root=root) == 0
    assert out.read_text() == "version=0.1.0a1\nprerelease=true\n"
    assert "0.1.0a1 (pre-release) is consistent" in capsys.readouterr().out


def test_final_release_needs_no_alpha_classifier(tmp_path):
    root = _project(
        tmp_path, version="1.0.0", classifiers='"Development Status :: 5 - Production/Stable"'
    )
    out = tmp_path / "gh_output"
    assert release_check.main(["v1.0.0", "--github-output", str(out)], root=root) == 0
    assert out.read_text() == "version=1.0.0\nprerelease=false\n"


def test_without_tag_checks_the_rest(tmp_path):
    assert release_check.problems(_project(tmp_path), None) == []


@pytest.mark.parametrize("tag", ["v0.1.0a2", "0.1.0a1", "v0.1.0", "v0.1.0-a1"])
def test_tag_mismatch_fails(tmp_path, tag, capsys):
    root = _project(tmp_path)
    assert release_check.main([tag], root=root) == 1
    assert "does not match pyproject version 0.1.0a1" in capsys.readouterr().err


@pytest.mark.parametrize("version", ["0.1.0a1", "1.0.0b2", "1.0.0rc1", "1.0.0.dev3"])
def test_prerelease_without_alpha_fails(tmp_path, version):
    root = _project(tmp_path, version=version, classifiers='"Framework :: Django"')
    [problem] = release_check.problems(root, f"v{version}")
    assert "is a pre-release but the classifiers lack" in problem


def test_license_classifier_fails(tmp_path):
    root = _project(tmp_path, classifiers=f'{ALPHA}, "License :: OSI Approved :: MIT License"')
    [problem] = release_check.problems(root, None)
    assert "License ::" in problem


def test_missing_changelog_fails(tmp_path):
    assert release_check.problems(_project(tmp_path, changelog=False), None) == [
        "CHANGELOG.md is missing"
    ]


def test_changelog_without_section_fails(tmp_path):
    root = _project(tmp_path, changelog=CHANGELOG.format(version="0.0.9"))
    [problem] = release_check.problems(root, None)
    assert "no '## [0.1.0a1] - YYYY-MM-DD' section" in problem


def test_changelog_section_without_date_fails(tmp_path):
    text = CHANGELOG.format(version="0.1.0a1").replace("## [0.1.0a1] - 2026-10-26", "## [0.1.0a1]")
    [problem] = release_check.problems(_project(tmp_path, changelog=text), None)
    assert "has no ' - YYYY-MM-DD' date" in problem


def test_section_prefix_is_not_a_match(tmp_path):
    # '## [0.1.0a10]' 은 0.1.0a1 의 절이 아니다
    root = _project(tmp_path, changelog=CHANGELOG.format(version="0.1.0a10"))
    [problem] = release_check.problems(root, None)
    assert "no '## [0.1.0a1]" in problem


def test_unreleased_entries_fail(tmp_path):
    text = CHANGELOG.format(version="0.1.0a1").replace(
        "## [Unreleased]\n", "## [Unreleased]\n\n- 아직 옮기지 않은 항목\n"
    )
    [problem] = release_check.problems(_project(tmp_path, changelog=text), None)
    assert "still has entries under [Unreleased]" in problem


@pytest.mark.parametrize("version", ["0.1.0-alpha1", "v0.1.0", "0.1.0A1", "1!0.1.0"])
def test_unnormalized_version_fails(tmp_path, version):
    [problem] = release_check.problems(_project(tmp_path, version=version), None)
    assert "not a normalized PEP 440 version" in problem


def test_repository_itself_is_consistent():
    # 저장소의 pyproject·CHANGELOG 가 지금 버전으로 릴리스할 수 있는 상태인지
    assert release_check.problems(ROOT, None) == []
