"""README 와 배포 프로필 문서의 링크가 깨지지 않았는지 확인한다 (#11).

- 상대 링크: 대상 파일·디렉터리가 있어야 한다.
- 앵커(``#...``): 대상 마크다운의 제목에서 GitHub 규칙으로 만든 앵커 중 하나여야 한다.
- 이 저장소의 이슈 링크: 링크 글자가 ``#N`` 이면 URL 의 번호와 같아야 한다.

외부 URL 은 네트워크 없이 확인할 수 없으므로 형식만 본다.
"""

import re
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs" / "profiles").glob("*.md"))]
ISSUE_URL = "https://github.com/itda-work/django-sqlite-ops/issues/"

FENCE = re.compile(r"^ *```.*?^ *```", flags=re.S | re.M)
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6}) +(.+?) *#* *$", flags=re.M)


def _prose(text: str) -> str:
    """코드 블록을 지운 본문. 줄 수는 그대로 둔다."""
    return FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def github_slug(heading: str) -> str:
    """GitHub 가 마크다운 제목에 붙이는 앵커(github-slugger 규칙).

    소문자로 바꾸고, 글자·숫자·결합 문자·``-``·``_``·공백만 남긴 뒤 공백을 ``-`` 로 바꾼다.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # 제목 안 링크는 글자만
    text = text.replace("`", "").lower()
    kept = []
    for ch in text:
        if ch in "-_ " or ch.isalnum() or unicodedata.category(ch).startswith("M"):
            kept.append(ch)
    return "".join(kept).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    found = set()
    for _, title in HEADING.findall(_prose(path.read_text(encoding="utf-8"))):
        slug = github_slug(title)
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 0
        found.add(slug)
    return found


def test_github_slug_rules():
    assert github_slug("boot CLI") == "boot-cli"
    assert github_slug("`sqlite_doctor`") == "sqlite_doctor"
    assert github_slug("`timeout` 과 `busy_timeout`") == "timeout-과-busy_timeout"
    assert github_slug("`urls.py` — 복제 헬스") == "urlspy--복제-헬스"
    assert github_slug("생애 첫 배포: `--init-new` 를 한 번 쓰고 끈다") == (
        "생애-첫-배포---init-new-를-한-번-쓰고-끈다"
    )
    assert github_slug("8. 배포 프로필과 채널 레이어") == "8-배포-프로필과-채널-레이어"


def test_duplicate_headings_get_suffixes(tmp_path):
    doc = tmp_path / "x.md"
    doc.write_text("# A\n\n## 함정\n\n```md\n## 함정\n```\n\n## 함정\n", encoding="utf-8")
    assert anchors(doc) == {"a", "함정", "함정-1"}


def _links():
    found = []
    for doc in DOCS:
        prose = INLINE_CODE.sub("", _prose(doc.read_text(encoding="utf-8")))
        for lineno, line in enumerate(prose.splitlines(), 1):
            for text, target in LINK.findall(line):
                found.append((doc, lineno, text, target))
    return found


LINKS = _links()
LOCAL = [link for link in LINKS if not re.match(r"[a-z]+:", link[3])]
ISSUES = [link for link in LINKS if link[3].startswith(ISSUE_URL)]


def _id(link):
    doc, lineno, _, target = link
    return f"{doc.relative_to(ROOT)}:{lineno}:{target}"


def test_documents_have_links():
    assert len(LOCAL) >= 30
    assert ISSUES


@pytest.mark.parametrize("link", LOCAL, ids=[_id(link) for link in LOCAL])
def test_local_link_resolves(link):
    doc, _, _, target = link
    path_part, _, fragment = target.partition("#")
    dest = (doc.parent / path_part).resolve() if path_part else doc
    assert dest.exists(), f"missing file: {dest}"
    assert dest.is_relative_to(ROOT), f"outside the repository: {dest}"
    if fragment:
        assert dest.suffix == ".md", f"anchor on a non-markdown target: {dest}"
        assert fragment in anchors(dest), f"no heading for #{fragment} in {dest.name}"


@pytest.mark.parametrize("link", ISSUES, ids=[_id(link) for link in ISSUES])
def test_issue_link_matches_its_text(link):
    _, _, text, target = link
    number = target.removeprefix(ISSUE_URL)
    assert number.isdigit(), target
    if re.fullmatch(r"#\d+", text):
        assert text == f"#{number}"
