"""scripts/release_check.py 가 태그·버전·CHANGELOG·분류의 어긋남을 거부하는지 확인한다 (#77)."""

import importlib.util
import io
import re
import tarfile
import tomllib
import zipfile
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


def test_duplicate_version_section_fails(tmp_path):
    text = CHANGELOG.format(version="0.1.0a1").replace(
        "## [0.1.0a1] - 2026-10-26\n", "## [0.1.0a1] - 2026-10-26\n\n## [0.1.0a1] - 2026-10-25\n"
    )
    [problem] = release_check.problems(_project(tmp_path, changelog=text), None)
    assert "has 2 '## [0.1.0a1]' sections" in problem


def test_unreleased_with_only_link_definitions_is_empty(tmp_path):
    # 첫 릴리스 전: 절이 [Unreleased] 하나뿐이고 그 아래가 링크 정의다. 정의는 항목이 아니다
    text = "# Changelog\n\n## [Unreleased]\n\n[Unreleased]: https://example.invalid/commits/main\n"
    [problem] = release_check.problems(_project(tmp_path, changelog=text), None)
    assert "no '## [0.1.0a1] - YYYY-MM-DD' section" in problem


@pytest.mark.parametrize(
    "version",
    ["0.1.0-alpha1", "v0.1.0", "0.1.0A1", "1!0.1.0", "0.01", "01.0", "1.0a01", "1.0.dev01"],
)
def test_unnormalized_version_fails(tmp_path, version):
    [problem] = release_check.problems(_project(tmp_path, version=version), None)
    assert "not a normalized PEP 440 version" in problem


def test_repository_itself_is_consistent():
    # 저장소의 pyproject·CHANGELOG 가 서로 맞는지. 릴리스 사이에는 [Unreleased] 에 항목이
    # 쌓이는 것이 정상이라 그 지적만 뺀다(릴리스 때는 release.yml 이 막는다, #91)
    found = release_check.problems(ROOT, None)
    assert [p for p in found if "still has entries under [Unreleased]" not in p] == []


# --dist: 빌드 산출물의 Version 과 PyPI 설명의 상대 링크 ------------------------------------

DESCRIPTION = (
    "# 제목\n\n[절대](https://example.invalid/x) [앵커](#제목) [메일](mailto:a@example.invalid)\n"
)


def _metadata(version, description):
    return f"Metadata-Version: 2.5\nName: example\nVersion: {version}\n\n{description}"


def _dist(tmp_path, version="0.1.0a1", description=DESCRIPTION, sdist_version=None):
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / f"example-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"example-{version}.dist-info/METADATA", _metadata(version, description))
    data = _metadata(sdist_version or version, description).encode("utf-8")
    with tarfile.open(dist / f"example-{version}.tar.gz", "w:gz") as sdist:
        # sdist 안 다른 PKG-INFO(하위 디렉터리)는 보지 않는다
        for name, body in ((f"example-{version}/PKG-INFO", data), ("example/tests/PKG-INFO", b"")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            sdist.addfile(info, io.BytesIO(body))
    return dist


def test_dist_consistent_passes(tmp_path):
    root = _project(tmp_path)
    assert release_check.main(["v0.1.0a1", "--dist", str(_dist(tmp_path))], root=root) == 0


def test_dist_without_tag_compares_with_pyproject(tmp_path, capsys):
    root = _project(tmp_path)
    assert release_check.main(["--dist", str(_dist(tmp_path, version="0.1.0"))], root=root) == 1
    assert "built Version '0.1.0' is not '0.1.0a1'" in capsys.readouterr().err


def test_dist_version_mismatch_fails(tmp_path):
    problems = release_check.dist_problems(_dist(tmp_path, sdist_version="0.1"), "0.1.0a1")
    assert problems == ["example-0.1.0a1.tar.gz: built Version '0.1' is not '0.1.0a1'"]


@pytest.mark.parametrize(
    "link",
    [
        "[설계](docs/DESIGN.md#3-범위)",
        "[디렉터리](docs/research/)",
        "![그림](img/a.png)",
        "[참조][r]\n\n[r]: docs/DECISIONS.md",
        '<a href="CHANGELOG.md">x</a>',
        '<img src="./img/a.png">',
        "[꺾쇠](<docs/a b.md>)",
    ],
)
def test_dist_relative_link_fails(tmp_path, link):
    [problem] = [
        p
        for p in release_check.dist_problems(
            _dist(tmp_path, description=DESCRIPTION + link), "0.1.0a1"
        )
        if p.startswith("example-0.1.0a1-py3")
    ]
    assert "the PyPI description has 1 relative links" in problem


@pytest.mark.parametrize(
    "image",
    [
        "![차트](https://github.com/itda-work/django-sqlite-ops/blob/main/docs/assets/a.svg)",
        '<img alt="x" src="https://github.com/o/r/blob/main/a.png">',
    ],
)
def test_dist_image_on_blob_page_fails(tmp_path, image):
    # 상대 이미지 경로는 fancy-pypi-readme 치환으로 이런 blob URL 이 된다(#83)
    [problem] = [
        p
        for p in release_check.dist_problems(
            _dist(tmp_path, description=DESCRIPTION + image), "0.1.0a1"
        )
        if p.startswith("example-0.1.0a1-py3")
    ]
    assert "1 images on GitHub blob pages" in problem


def test_dist_raw_image_and_blob_link_pass(tmp_path):
    # raw URL 이미지와 blob 페이지로 가는 일반 링크는 괜찮다
    text = DESCRIPTION + (
        "![차트](https://raw.githubusercontent.com/o/r/main/docs/assets/a.svg)\n"
        "[문서](https://github.com/o/r/blob/main/docs/DESIGN.md)\n"
    )
    assert release_check.dist_problems(_dist(tmp_path, description=text), "0.1.0a1") == []


def test_readme_images_are_raw_urls_of_tracked_files():
    # README 이미지는 PyPI 에서도 보이게 raw 절대 URL 로 건다. 상대 경로는 치환으로 blob 이 된다
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    images = [m[1] for pattern in release_check._IMAGE_TARGETS for m in pattern.finditer(readme)]
    raw = "https://raw.githubusercontent.com/itda-work/django-sqlite-ops/main/"
    # 이미지를 모두 지워도 통과하지 않게 README 차트를 기대값으로 둔다(#83 리뷰 P3-4)
    assert sorted(images) == [
        raw + "docs/assets/conn-max-age.svg",
        raw + "docs/assets/dbconfig.svg",
    ]
    for target in images:
        assert target.startswith(raw), target
        assert (ROOT / target.removeprefix(raw)).is_file(), target


def test_dist_needs_one_wheel_and_one_sdist(tmp_path):
    dist = _dist(tmp_path)
    (dist / "example-0.1.0a1.tar.gz").unlink()
    [problem] = release_check.dist_problems(dist, "0.1.0a1")
    assert "exactly one wheel and one sdist, found 1 and 0" in problem


def test_dist_with_a_leftover_wheel_fails(tmp_path):
    # 이전 빌드의 wheel 이 남아 있으면 그것까지 올라간다
    dist = _dist(tmp_path)
    (dist / "example-0.0.9-py3-none-any.whl").write_bytes(
        (dist / "example-0.1.0a1-py3-none-any.whl").read_bytes()
    )
    [problem] = release_check.dist_problems(dist, "0.1.0a1")
    assert "exactly one wheel and one sdist, found 2 and 1" in problem


def test_pypi_readme_substitution_makes_every_readme_link_absolute():
    # pyproject 의 fancy-pypi-readme 치환을 README 에 그대로 적용하면 상대 링크가 남지 않는다.
    # 빌드 없이(빌드 플러그인을 받지 않고) 같은 re.sub 로 확인한다.
    # 실제 산출물은 release.yml 의 --dist 가 본다
    hook = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"][
        "metadata"
    ]["hooks"]["fancy-pypi-readme"]
    [fragment] = hook["fragments"]
    readme = (ROOT / fragment["path"]).read_text(encoding="utf-8")
    before = release_check.relative_links(readme)
    assert len(before) > 0  # GitHub 에서 보는 README 는 상대 링크를 그대로 쓴다
    text = readme
    for sub in hook["substitutions"]:
        text = re.sub(sub["pattern"], sub["replacement"], text)
    assert release_check.relative_links(text) == []
    base = "https://github.com/itda-work/django-sqlite-ops/blob/main/"
    assert text.count(f"]({base}") == len(before)
    # 앵커·절대 링크는 건드리지 않는다
    for kept in re.findall(r"\]\((?:#|https?:|mailto:)[^)]*\)", readme):
        assert readme.count(kept) == text.count(kept)
