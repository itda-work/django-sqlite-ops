"""Litestream 의 새 정식 릴리스가 버전 게이트에서 검증되지 않았으면 이 저장소에 이슈를 연다 (#104).

`.github/workflows/litestream-watch.yml` 이 주 1회 돌린다. 로컬에서는:

    python3 scripts/litestream_watch.py --dry-run                   # 실제 최신 판, 이슈 없음
    python3 scripts/litestream_watch.py --dry-run --latest v0.5.19  # 주어진 판으로 흐름만

`--latest` 는 `--dry-run` 과 함께만 받는다(시험용 판으로 실제 이슈를 만들지 않는다).

- 검증한 판은 `django_sqlite_ops.boot.litestream.VERIFIED_VERSIONS` 를 그대로 읽는다(boot 는
  Django 를 import 하지 않으므로 의존성 없이 돈다). 이 파일에 판 번호를 적지 않는다.
- 최신 판은 `GET /repos/benbjohnson/litestream/releases/latest` 의 `tag_name` 이다(정식 릴리스만).
  상대 저장소는 읽기만 한다(D-5).
- 태그는 `^v?\\d+\\.\\d+\\.\\d+$` 꼴만 받는다. 아니면 값을 repr 로 찍고 실패한다. 검증된 값만
  제목·본문·인자에 쓰고, 셸을 거치지 않는다(`gh` 를 인자 목록으로 부른다).
- 같은 제목의 이슈가 열림·닫힘 어느 쪽에든 있으면 만들지 않는다. `--dry-run` 은 읽기(최신 판·
  중복 검색)까지 하고 만들기만 건너뛴다.
- API·`gh` 실패는 exit 1 이다. 조용히 성공하지 않는다.

표준 라이브러리와 `gh`(GitHub 러너에 있다)만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

UPSTREAM = "benbjohnson/litestream"
TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
GH_TIMEOUT = 60


class WatchError(Exception):
    pass


def parse_tag(tag: object) -> str:
    """릴리스 태그를 `X.Y.Z` 로 바꾼다. 다른 꼴은 repr 을 담아 거부한다."""
    # re.match 의 `$` 는 끝 줄바꿈 하나를 허용하므로 fullmatch 를 쓴다(`\n` 도 거부)
    m = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if m is None or not tag.isascii():
        raise WatchError(f"unexpected release tag {tag!r} (expected vX.Y.Z)")
    return ".".join(str(int(p)) for p in m.groups())


def _key(version: str) -> tuple[int, int, int]:
    return tuple(int(p) for p in version.split("."))  # type: ignore[return-value]


def classify(latest: str, verified: Iterable[str]) -> str:
    """`verified` (게이트에 있음) / `new` (가장 높은 검증 판보다 높음) / `older`."""
    verified = set(verified)
    if latest in verified:
        return "verified"
    if _key(latest) > max(_key(v) for v in verified):
        return "new"
    return "older"


def issue_title(version: str) -> str:
    return f"Litestream {version} is not verified by the version gate"


def release_url(tag: str) -> str:
    return f"https://github.com/{UPSTREAM}/releases/tag/{tag}"


def issue_body(version: str, tag: str, verified: Iterable[str]) -> str:
    verified_text = ", ".join(sorted(verified, key=_key))
    gate = "`django_sqlite_ops/boot/litestream.py` 의 `VERIFIED_VERSIONS`"
    fixtures = "`tests/fixtures/make_litestream_fixtures.sh`"
    workflow = "`.github/workflows/litestream-watch.yml`(`scripts/litestream_watch.py`)"
    # 릴리스 주소는 링크로 걸지 않고 코드 스팬 안의 평문으로 둔다
    # (상대 저장소에 역참조를 남기지 않는다)
    lines = [
        "## 배경",
        "",
        f"Litestream 의 최신 정식 릴리스가 `{version}` 이다(`{release_url(tag)}`).",
        f"버전 게이트({gate})는 {verified_text} 만 받으므로,",
        "이 판을 설치한 사용자의 boot 는 거부된다.",
        "",
        f"이 이슈는 {workflow}이 자동으로 열었다(#104).",
        "",
        "## 할 일 (#99 의 절차)",
        "",
        f"- [ ] 새 판의 바이너리로 {fixtures} 를 돌려 "
        f"`tests/fixtures/litestream-{version}/` 를 만든다",
        "- [ ] 직전 검증 판의 fixture 와 차이를 확인한다(출력 형식·동작 변화)",
        "- [ ] 회귀 랩을 새 판으로 돌린다(`lab/README.md`)",
        "- [ ] 문제가 없으면 `VERIFIED_VERSIONS` 와 CI `litestream` 잡의 매트릭스에 넣는다. "
        "문서(DESIGN §5·README·CLAUDE.md)를 함께 고친다",
        "",
        "## 검증 상태",
        "",
        "- 새 릴리스: 코드상 확인(GitHub API `releases/latest` 의 `tag_name`)",
        "- 동작 차이: 미검증",
    ]
    return "\n".join(lines) + "\n"


def find_existing(title: str, issues: Iterable[dict]) -> dict | None:
    """제목이 정확히 같은 이슈(열림·닫힘 무관)를 돌려준다."""
    for issue in issues:
        if issue.get("title") == title:
            return issue
    return None


def run(
    *,
    verified: Iterable[str],
    latest_tag: Callable[[], object],
    search_issues: Callable[[str], list[dict]],
    create_issue: Callable[[str, str], str],
    dry_run: bool,
) -> int:
    try:
        verified = {parse_tag(v) for v in verified}
        if not verified:
            raise WatchError("VERIFIED_VERSIONS is empty")
        tag = latest_tag()
        version = parse_tag(tag)
        state = classify(version, verified)
        print(f"latest Litestream release: {version} (tag {tag!r})")
        print(f"verified versions: {', '.join(sorted(verified, key=_key))}")
        if state == "verified":
            print(f"{version} is verified; nothing to do")
            return 0
        if state == "older":
            raise WatchError(
                f"latest release {version} is older than every verified version and not in the gate"
            )
        title = issue_title(version)
        existing = find_existing(title, search_issues(title))
        if existing is not None:
            print(
                f"{version} is not verified; issue #{existing.get('number')} "
                f"({existing.get('state')}) already exists: {title!r}"
            )
            return 0
        body = issue_body(version, tag, verified)
        if dry_run:
            print(f"{version} is not verified; dry-run: would create issue {title!r}")
            print("--- body ---")
            print(body, end="")
            return 0
        url = create_issue(title, body)
        print(f"{version} is not verified; created issue: {url}")
        return 0
    except WatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


# --- 실제 호출 --------------------------------------------------------------


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            cwd=ROOT,
        )
    except FileNotFoundError as e:
        raise WatchError("gh not found") from e
    except subprocess.TimeoutExpired as e:
        raise WatchError(f"gh {args[0]} timed out after {GH_TIMEOUT}s") from e
    if proc.returncode != 0:
        raise WatchError(
            f"gh {' '.join(args[:2])} failed (rc {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _repo_args() -> list[str]:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return ["--repo", repo] if repo else []


def gh_latest_tag() -> object:
    out = _gh(["api", f"repos/{UPSTREAM}/releases/latest"])
    try:
        return json.loads(out)["tag_name"]
    except (ValueError, KeyError, TypeError) as e:
        raise WatchError(f"unexpected releases/latest response: {out[:200]!r}") from e


def gh_search_issues(title: str) -> list[dict]:
    out = _gh(
        [
            "issue",
            "list",
            *_repo_args(),
            "--state",
            "all",
            "--search",
            f'in:title "{title}"',
            "--json",
            "number,title,state",
            "--limit",
            "100",
        ]
    )
    try:
        issues = json.loads(out)
    except ValueError as e:
        raise WatchError(f"unexpected gh issue list output: {out[:200]!r}") from e
    if not isinstance(issues, list):
        raise WatchError(f"unexpected gh issue list output: {out[:200]!r}")
    return issues


def gh_create_issue(title: str, body: str) -> str:
    return _gh(
        ["issue", "create", *_repo_args(), "--title", title, "--body-file", "-"], stdin=body
    ).strip()


def load_verified() -> frozenset[str]:
    sys.path.insert(0, str(ROOT))
    from django_sqlite_ops.boot.litestream import VERIFIED_VERSIONS

    return VERIFIED_VERSIONS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="이슈를 만들지 않고 할 일만 출력한다"
    )
    parser.add_argument(
        "--latest",
        metavar="TAG",
        help="API 대신 이 태그를 최신 판으로 쓴다(시험용, --dry-run 과 함께만)",
    )
    args = parser.parse_args(argv)
    # 시험용 판으로 공개 저장소에 실제 이슈를 만들지 않게, 아무것도 부르기 전에 거부한다
    if args.latest is not None and not args.dry_run:
        parser.error("--latest is for testing and requires --dry-run")
    latest = (lambda: args.latest) if args.latest is not None else gh_latest_tag
    return run(
        verified=load_verified(),
        latest_tag=latest,
        search_issues=gh_search_issues,
        create_issue=gh_create_issue,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
