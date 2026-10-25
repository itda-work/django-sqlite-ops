"""릴리스 전에 태그·pyproject 버전·CHANGELOG·분류가 서로 맞는지 검사한다 (#77).

release.yml 이 빌드 전에 태그 이름으로 돌린다. 로컬에서는 태그 없이 돌려 버전과 CHANGELOG 만 본다:

    python3 scripts/release_check.py            # 버전·CHANGELOG·분류
    python3 scripts/release_check.py v0.1.0a1   # 태그까지

`--github-output PATH` 를 주면 `version=`·`prerelease=true|false` 를 그 파일에 덧붙인다
(GitHub Actions 의 `$GITHUB_OUTPUT`). 한 번 올린 버전은 PyPI 에서 다시 쓸 수 없으므로,
어긋나면 빌드 전에 거부한다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALPHA = "Development Status :: 3 - Alpha"
# PEP 440 공개 버전(에포크·로컬 없음)의 정규화된 꼴만 받는다.
# 예: 0.1.0a1, 1.0.0rc2, 1.0.0.post1, 1.0.0.dev3
VERSION = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"
    r"(?P<pre>(?:a|b|rc)\d+)?"
    r"(?P<post>\.post\d+)?"
    r"(?P<dev>\.dev\d+)?$"
)
SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\](?P<rest>.*)$", re.M)
DATED = re.compile(r"^ - \d{4}-\d{2}-\d{2}$")


def is_prerelease(version: str) -> bool:
    match = VERSION.match(version)
    if match is None:
        raise ValueError(f"not a normalized PEP 440 version: {version!r}")
    return bool(match["pre"] or match["dev"])


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
    args = parser.parse_args(argv)

    found = problems(root, args.tag)
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
