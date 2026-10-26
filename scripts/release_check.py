"""릴리스 전에 태그·pyproject 버전·CHANGELOG·분류가 서로 맞는지 검사한다 (#77).

release.yml 이 빌드 전에 태그 이름으로 돌린다. 로컬에서는 태그 없이 돌려 버전과 CHANGELOG 만 본다:

    python3 scripts/release_check.py            # 버전·CHANGELOG·분류
    python3 scripts/release_check.py v0.1.0a1   # 태그까지
    python3 scripts/release_check.py v0.1.0a1 --dist dist   # uv build 뒤: 산출물까지

`--dist DIR` 는 빌드된 wheel 의 METADATA 와 sdist 의 PKG-INFO 를 읽어 `Version` 이 태그(없으면
pyproject version)와 같은지, PyPI 설명에 상대 링크가 남지 않았는지 본다. PyPI 는 설명의 상대
링크를 저장소로 풀지 않으므로 pyproject 의 fancy-pypi-readme 치환이 모두 바꿨어야 한다.
이미지는 GitHub blob 페이지(HTML)를 가리키면 깨지므로 그것도 거부한다(#83,
raw.githubusercontent.com 을 쓴다).
`--github-output PATH` 를 주면 `version=`·`prerelease=true|false` 를 그 파일에 덧붙인다
(GitHub Actions 의 `$GITHUB_OUTPUT`). 한 번 올린 버전은 PyPI 에서 다시 쓸 수 없으므로,
어긋나면 빌드 전에 거부한다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import argparse
import email
import email.message
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALPHA = "Development Status :: 3 - Alpha"
# PEP 440 공개 버전(에포크·로컬 없음)의 정규화된 꼴만 받는다. 앞자리 0(`0.01`)은 정규형이 아니다.
# 예: 0.1.0a1, 1.0.0rc2, 1.0.0.post1, 1.0.0.dev3
_N = r"(?:0|[1-9]\d*)"
VERSION = re.compile(
    rf"^(?P<release>{_N}(?:\.{_N})*)"
    rf"(?P<pre>(?:a|b|rc){_N})?"
    rf"(?P<post>\.post{_N})?"
    rf"(?P<dev>\.dev{_N})?$"
)
# 마크다운의 링크 대상: 인라인 `](x)`·`](<x>)`, 참조 정의 `[id]: x`, 원시 HTML 의 href·src
_LINK_TARGETS = (
    re.compile(r"\]\(\s*<?([^)\s>]+)"),
    re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)", re.M),
    re.compile(r"""(?:href|src)\s*=\s*["']([^"']*)["']""", re.I),
)
# 스킴(https:·mailto: 등)이나 문서 안 앵커(#)로 시작하면 상대 링크가 아니다
_ABSOLUTE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|#)")
# 이미지 대상: 마크다운 `![alt](x)`, 원시 HTML 의 img src
_IMAGE_TARGETS = (
    re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)"),
    re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']*)["']""", re.I),
)
# GitHub 의 blob 페이지는 HTML 이라 이미지로 열리지 않는다(#83). raw.githubusercontent.com 을 쓴다
_BLOB = re.compile(r"^https?://github\.com/[^/]+/[^/]+/blob/")
SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\](?P<rest>.*)$", re.M)
DATED = re.compile(r"^ - \d{4}-\d{2}-\d{2}$")


def is_prerelease(version: str) -> bool:
    match = VERSION.match(version)
    if match is None:
        raise ValueError(f"not a normalized PEP 440 version: {version!r}")
    return bool(match["pre"] or match["dev"])


def relative_links(markdown: str) -> list[str]:
    """PyPI 설명에서 깨질 상대 링크 대상을 나온 순서대로 돌려준다."""
    found = []
    for pattern in _LINK_TARGETS:
        found += [(m.start(1), m[1]) for m in pattern.finditer(markdown)]
    return [target for _, target in sorted(found) if not _ABSOLUTE.match(target)]


def blob_images(markdown: str) -> list[str]:
    """PyPI·GitHub 에서 깨질 이미지 대상(GitHub blob 페이지 URL)을 나온 순서대로 돌려준다.

    상대 이미지 경로는 fancy-pypi-readme 치환이 ``blob/main`` URL 로 바꾸므로 여기에 걸린다.
    잡지 않는 것: 참조식 이미지(``![a][ref]`` + ``[ref]: …/blob/…``), ``www.github.com`` 호스트.
    지금 README 에는 그런 꼴이 없다(README 이미지 경로는 ``tests/test_release_check.py`` 가
    따로 본다).
    """
    found = []
    for pattern in _IMAGE_TARGETS:
        found += [(m.start(1), m[1]) for m in pattern.finditer(markdown)]
    return [target for _, target in sorted(found) if _BLOB.match(target)]


def _metadata(path: Path) -> email.message.Message:
    # core metadata 는 UTF-8 이다. message_from_bytes 는 본문의 비 ASCII 를 깨뜨리므로 먼저 푼다
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            [name] = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
            return email.message_from_string(wheel.read(name).decode("utf-8"))
    with tarfile.open(path) as sdist:
        # 최상위 디렉터리 바로 아래의 PKG-INFO 만 본다(tests 아래 같은 이름이 있어도 섞이지 않게)
        [member] = [
            m for m in sdist.getmembers() if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")
        ]
        return email.message_from_string(sdist.extractfile(member).read().decode("utf-8"))


def dist_problems(dist: Path, expected: str) -> list[str]:
    """빌드 산출물(wheel 하나·sdist 하나)의 Version 과 PyPI 설명의 상대 링크를 검사한다."""
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return [
            f"{dist} must hold exactly one wheel and one sdist,"
            f" found {len(wheels)} and {len(sdists)}"
        ]
    found = []
    for path in (*wheels, *sdists):
        try:
            metadata = _metadata(path)
        except (ValueError, KeyError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            found.append(f"{path.name}: cannot read the metadata ({exc})")
            continue
        if metadata["Version"] != expected:
            found.append(f"{path.name}: built Version {metadata['Version']!r} is not {expected!r}")
        links = relative_links(metadata.get_payload())
        if links:
            shown = ", ".join(links[:5]) + (", ..." if len(links) > 5 else "")
            found.append(
                f"{path.name}: the PyPI description has {len(links)} relative links ({shown})"
            )
        images = blob_images(metadata.get_payload())
        if images:
            shown = ", ".join(images[:5]) + (", ..." if len(images) > 5 else "")
            found.append(
                f"{path.name}: the PyPI description has {len(images)} images on GitHub blob"
                f" pages, which do not render; use raw.githubusercontent.com URLs ({shown})"
            )
    return found


def problems(root: Path, tag: str | None) -> list[str]:
    """어긋난 점을 모두 모아 돌려준다. 비었으면 릴리스할 수 있다."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project.get("version")
    if not isinstance(version, str):
        return ["pyproject.toml [project] has no static version"]
    if VERSION.match(version) is None:
        return [f"pyproject version {version!r} is not a normalized PEP 440 version"]
    found = []

    if tag is not None and tag != f"v{version}":
        found.append(
            f"tag {tag!r} does not match pyproject version {version} (expected v{version})"
        )

    classifiers = project.get("classifiers", [])
    if is_prerelease(version) and ALPHA not in classifiers:
        found.append(f"{version} is a pre-release but the classifiers lack {ALPHA!r}")
    if any(c.startswith("License :: ") for c in classifiers):
        # PEP 639: License-Expression 와 함께 쓸 수 없다. PyPI 가 업로드를 거부한다
        found.append("drop the 'License :: ...' classifiers; the license field already says it")

    changelog_path = root / "CHANGELOG.md"
    if not changelog_path.is_file():
        return [*found, "CHANGELOG.md is missing"]
    changelog = changelog_path.read_text(encoding="utf-8")
    sections = list(SECTION.finditer(changelog))
    matching = [s for s in sections if s["version"] == version]
    if not matching:
        found.append(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section")
    else:
        if len(matching) > 1:
            found.append(f"CHANGELOG.md has {len(matching)} '## [{version}]' sections")
        if DATED.match(matching[0]["rest"]) is None:
            found.append(f"CHANGELOG.md '## [{version}]' has no ' - YYYY-MM-DD' date")
    for i, section in enumerate(sections):
        if section["version"] != "Unreleased":
            continue
        end = sections[i + 1].start() if i + 1 < len(sections) else len(changelog)
        body = changelog[section.end() : end]
        # 아래쪽 링크 정의([x]: URL)는 절의 내용이 아니다
        body = re.sub(r"^\[[^\]]+\]: \S+$", "", body, flags=re.M)
        if body.strip():
            found.append(
                f"CHANGELOG.md still has entries under [Unreleased]; move them to [{version}]"
            )
    return found


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", nargs="?", help="the git tag being released, e.g. v0.1.0a1")
    parser.add_argument("--github-output", type=Path, help="append version= and prerelease=")
    parser.add_argument("--dist", type=Path, help="also check the built wheel and sdist in DIR")
    args = parser.parse_args(argv)

    found = problems(root, args.tag)
    if args.dist is not None:
        if args.tag is not None:
            expected = args.tag.removeprefix("v")
        else:
            expected = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
                "project"
            ].get("version")
        found += dist_problems(args.dist, expected)
    for problem in found:
        print(f"release check: {problem}", file=sys.stderr)
    if found:
        return 1
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    prerelease = is_prerelease(version)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(f"version={version}\nprerelease={str(prerelease).lower()}\n")
    kind = "pre-release" if prerelease else "release"
    print(f"release check: {version} ({kind}) is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
