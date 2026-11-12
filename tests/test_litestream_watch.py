"""scripts/litestream_watch.py 가 검증하지 않은 Litestream 릴리스를 감지하는지 확인한다 (#104).

네트워크를 쓰지 않는다. API 호출은 가짜 함수를 주입한다.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from django_sqlite_ops.boot.litestream import VERIFIED_VERSIONS

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "litestream_watch", ROOT / "scripts/litestream_watch.py"
)
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)

VERIFIED = frozenset({"0.5.17", "0.5.18"})


# --- 태그 파싱 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("v0.5.19", "0.5.19"), ("0.5.19", "0.5.19"), ("v1.10.0", "1.10.0")],
)
def test_parse_tag_accepts_release_tags(tag, expected):
    assert watch.parse_tag(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "v0.6.0-beta1",
        "v0.5.19-rc1",
        "0.5",
        "",
        "latest",
        "v0.5.19; rm -rf /",
        "v0.5.19\n",
        "$(id)",
        "v0.5.19 ",
        "vv0.5.19",
        "v\u0660.5.19",  # 아랍 숫자 0 — \d 는 유니코드 숫자도 받는다
        None,
        5,
    ],
)
def test_parse_tag_rejects_other_shapes(tag):
    with pytest.raises(watch.WatchError) as exc:
        watch.parse_tag(tag)
    # 값은 repr 로만 나온다(줄바꿈·제어 문자가 로그를 꾸미지 못한다)
    assert repr(tag) in str(exc.value)


# --- 판 비교 ---------------------------------------------------------------


def test_classify_verified():
    assert watch.classify("0.5.18", VERIFIED) == "verified"


@pytest.mark.parametrize("latest", ["0.5.19", "0.6.0", "1.0.0", "0.5.100"])
def test_classify_new_release(latest):
    assert watch.classify(latest, VERIFIED) == "new"


@pytest.mark.parametrize("latest", ["0.5.16", "0.4.99"])
def test_classify_older_than_verified(latest):
    assert watch.classify(latest, VERIFIED) == "older"


def test_classify_compares_numerically_not_lexically():
    # 문자열 비교면 "0.5.9" > "0.5.18" 이다
    assert watch.classify("0.5.9", VERIFIED) == "older"
    assert watch.classify("0.5.100", VERIFIED) == "new"


# --- 제목·본문 -------------------------------------------------------------


def test_issue_title_fixed_shape():
    assert watch.issue_title("0.5.19") == "Litestream 0.5.19 is not verified by the version gate"


def test_issue_body_mentions_release_as_plain_code_span():
    body = watch.issue_body("0.5.19", "v0.5.19", VERIFIED)
    url = "https://github.com/benbjohnson/litestream/releases/tag/v0.5.19"
    # 링크가 아니라 코드 스팬 안의 평문이다(상대 저장소에 역참조를 남기지 않는다)
    assert f"`{url}`" in body
    assert f"]({url})" not in body
    assert f"<{url}>" not in body
    assert body.count(url) == 1
    assert "0.5.17" in body and "0.5.18" in body
    assert "#99" in body
    assert "make_litestream_fixtures.sh" in body
    assert "VERIFIED_VERSIONS" in body


# --- 중복 판정 -------------------------------------------------------------


TITLE = "Litestream 0.5.19 is not verified by the version gate"


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_existing_issue_open_or_closed_is_duplicate(state):
    issues = [{"number": 120, "title": TITLE, "state": state}]
    assert watch.find_existing(TITLE, issues) == issues[0]


def test_similar_title_is_not_duplicate():
    issues = [
        {
            "number": 1,
            "title": "Litestream 0.5.19 is not verified by the version gate (again)",
            "state": "OPEN",
        },
        {
            "number": 2,
            "title": "Litestream 0.5.1 is not verified by the version gate",
            "state": "OPEN",
        },
    ]
    assert watch.find_existing(TITLE, issues) is None


# --- 전체 흐름 -------------------------------------------------------------


class Fake:
    def __init__(self, tag="v0.5.19", issues=()):
        self.tag = tag
        self.issues = list(issues)
        self.searched = []
        self.created = []

    def latest_tag(self):
        return self.tag

    def search_issues(self, title):
        self.searched.append(title)
        return self.issues

    def create_issue(self, title, body):
        self.created.append((title, body))
        return "https://github.com/itda-work/django-sqlite-ops/issues/999"


def _run(fake, *, dry_run=False, verified=VERIFIED, latest=None):
    return watch.run(
        verified=verified,
        latest_tag=fake.latest_tag if latest is None else (lambda: latest),
        search_issues=fake.search_issues,
        create_issue=fake.create_issue,
        dry_run=dry_run,
    )


def test_run_verified_does_nothing(capsys):
    fake = Fake(tag="v0.5.18")
    assert _run(fake) == 0
    assert fake.searched == [] and fake.created == []
    assert "0.5.18" in capsys.readouterr().out


def test_run_new_release_creates_issue(capsys):
    fake = Fake()
    assert _run(fake) == 0
    assert fake.searched == [TITLE]
    assert [t for t, _ in fake.created] == [TITLE]
    assert "issues/999" in capsys.readouterr().out


def test_run_new_release_with_existing_issue_does_not_create(capsys):
    fake = Fake(issues=[{"number": 120, "title": TITLE, "state": "CLOSED"}])
    assert _run(fake) == 0
    assert fake.created == []
    assert "#120" in capsys.readouterr().out


def test_run_dry_run_does_not_create(capsys):
    fake = Fake()
    assert _run(fake, dry_run=True) == 0
    assert fake.created == []
    # dry-run 도 읽기(중복 검색)는 한다. 만들기만 건너뛴다
    assert fake.searched == [TITLE]
    out = capsys.readouterr().out
    assert "dry-run" in out and TITLE in out


def test_run_older_release_fails(capsys):
    fake = Fake(tag="v0.5.16")
    assert _run(fake) == 1
    assert fake.created == []
    assert "older" in capsys.readouterr().err


def test_run_bad_tag_fails_with_repr(capsys):
    fake = Fake(tag="v0.6.0-beta1")
    assert _run(fake) == 1
    assert fake.searched == [] and fake.created == []
    assert repr("v0.6.0-beta1") in capsys.readouterr().err


def test_run_api_error_fails(capsys):
    def boom():
        raise watch.WatchError("GitHub API failed: HTTP 403 rate limit")

    fake = Fake()
    assert (
        watch.run(
            verified=VERIFIED,
            latest_tag=boom,
            search_issues=fake.search_issues,
            create_issue=fake.create_issue,
            dry_run=False,
        )
        == 1
    )
    assert "rate limit" in capsys.readouterr().err


def test_run_search_error_fails(capsys):
    fake = Fake()

    def boom(title):
        raise watch.WatchError("gh issue list failed")

    fake.search_issues = boom
    assert _run(fake) == 1
    assert fake.created == []


def test_run_invalid_verified_version_fails(capsys):
    fake = Fake()
    assert _run(fake, verified=frozenset({"0.5.x"})) == 1
    assert fake.created == []


# --- 실제 게이트와 CLI ------------------------------------------------------


def test_reads_real_verified_versions():
    # 워크플로·스크립트에 판 번호를 적지 않는다: 게이트 상수를 그대로 읽는다
    assert watch.load_verified() == VERIFIED_VERSIONS
    for v in VERIFIED_VERSIONS:
        assert watch.parse_tag(v) == v


def test_cli_latest_override_dry_run_without_django():
    # 워크플로는 의존성 없이 python3 로 돈다. boot 는 Django 를 import 하지 않는다
    prelude = (
        "import sys, runpy\n"
        "class B:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'django' or name.startswith('django.'):\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, B())\n"
        "sys.argv = ['litestream_watch.py', '--dry-run', '--latest', 'v0.5.18']\n"
        f"runpy.run_path({str(ROOT / 'scripts/litestream_watch.py')!r}, run_name='__main__')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", prelude], capture_output=True, text=True, timeout=60
    )
    # 검증된 판이면 이슈 검색도 하지 않으므로 네트워크 없이 끝난다
    assert proc.returncode == 0, proc.stderr
    assert "0.5.18" in proc.stdout and "verified" in proc.stdout


def test_cli_latest_override_rejects_bad_value():
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/litestream_watch.py"),
            "--dry-run",
            "--latest",
            "v1.0.0-rc1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert repr("v1.0.0-rc1") in proc.stderr


def test_cli_latest_without_dry_run_is_rejected(monkeypatch, capsys):
    # --latest 는 시험용이다. --dry-run 없이 주면 가짜 판으로 실제 이슈를 만들 수 있으므로 거부한다
    called = []
    monkeypatch.setattr(watch, "gh_latest_tag", lambda: called.append("latest") or "v0.5.18")
    monkeypatch.setattr(watch, "gh_search_issues", lambda t: called.append("search") or [])
    monkeypatch.setattr(watch, "gh_create_issue", lambda t, b: called.append("create") or "url")
    monkeypatch.setattr(watch, "load_verified", lambda: called.append("verified") or VERIFIED)
    with pytest.raises(SystemExit) as exc:
        watch.main(["--latest", "v9.9.9"])
    assert exc.value.code == 2
    assert called == []
    assert "--dry-run" in capsys.readouterr().err
