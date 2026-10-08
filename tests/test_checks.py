"""정적 시스템 체크 W001–W003·E001 (DESIGN §6-1).

Django 설정은 프로세스당 한 번만 정할 수 있어서, 시나리오마다 새 인터프리터에서
``settings.configure()`` → ``django.setup()`` 을 한다(기존 테스트와 같은 방식, 새 의존성 없음).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _nodjango import assert_runs_without_django

from django_sqlite_ops.database import PROFILES, sqlite_database

ROOT = Path(__file__).resolve().parent.parent

RUN_CHECKS_SCRIPT = """
import json
import sys

import django
from django.conf import settings

config = json.loads(sys.argv[1])
deploy = config.pop("_deploy")
settings.configure(INSTALLED_APPS=["django_sqlite_ops"], USE_TZ=True, **config)
django.setup()

from django.core.checks import run_checks

messages = run_checks(include_deployment_checks=deploy)
print(json.dumps(sorted([m.id, m.obj] for m in messages if m.id.startswith("sqlite_ops."))))
"""


def run_checks(databases=None, *, deploy=True, **extra):
    config = {"DATABASES": databases or {}, "_deploy": deploy, **extra}
    result = subprocess.run(
        [sys.executable, "-c", RUN_CHECKS_SCRIPT, json.dumps(config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [tuple(item) for item in json.loads(result.stdout)]


def raw(name="/srv/app/app.sqlite3", **options):
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": name, "OPTIONS": options}


WAL = "PRAGMA journal_mode=WAL"
VFS = "file:/srv/app/app.sqlite3?vfs=litestream&mode=ro"


@pytest.mark.parametrize("profile", PROFILES)
def test_sqlite_database_output_has_no_warnings(profile):
    db = sqlite_database("/srv/app/app.sqlite3", profile=profile)
    assert run_checks({"default": db}, SQLITE_OPS={"PROFILE": profile}) == []


def test_default_profile_used_when_setting_missing():
    assert run_checks({"default": sqlite_database("/srv/app/app.sqlite3")}) == []


# W001: transaction_mode


@pytest.mark.parametrize("mode", ["IMMEDIATE", "immediate", "Immediate"])
def test_w001_negative(mode):
    assert run_checks({"default": raw(transaction_mode=mode, init_command=WAL)}) == []


@pytest.mark.parametrize(
    "options", [{}, {"transaction_mode": "DEFERRED"}, {"transaction_mode": None}]
)
def test_w001_positive(options):
    db = raw(init_command=WAL, **options)
    assert run_checks({"default": db}) == [("sqlite_ops.W001", "default")]


def test_w001_w002_only_with_deploy():
    assert run_checks({"default": raw()}, deploy=False) == []
    assert run_checks({"default": raw()}, deploy=True) == [
        ("sqlite_ops.W001", "default"),
        ("sqlite_ops.W002", "default"),
    ]


def test_options_missing_entirely():
    db = {"ENGINE": "django.db.backends.sqlite3", "NAME": "/srv/app/app.sqlite3"}
    assert run_checks({"default": db}) == [
        ("sqlite_ops.W001", "default"),
        ("sqlite_ops.W002", "default"),
    ]


# W002: journal_mode in init_command


@pytest.mark.parametrize(
    "init_command",
    [
        "PRAGMA journal_mode=WAL",
        "PRAGMA journal_mode = wal",
        "  pragma   JOURNAL_MODE=Wal  ",
        "PRAGMA main.journal_mode=WAL",
        "PRAGMA journal_mode='wal'",
        "PRAGMA journal_mode(WAL)",
        "PRAGMA synchronous=NORMAL;PRAGMA journal_mode=WAL;PRAGMA busy_timeout=5000",
        "PRAGMA journal_mode=DELETE;PRAGMA journal_mode=WAL",
        "PRAGMA journal_mode=WAL;",
    ],
)
def test_w002_negative(init_command):
    db = raw(transaction_mode="IMMEDIATE", init_command=init_command)
    assert run_checks({"default": db}) == []


@pytest.mark.parametrize(
    "init_command",
    [
        None,
        "",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA journal_mode=DELETE",
        "PRAGMA journal_mode=WAL;PRAGMA journal_mode=DELETE",  # 마지막 값이 이긴다
        "PRAGMA temp.journal_mode=WAL",  # main 이 아닌 스키마
        "PRAGMA journal_mode",  # 조회일 뿐
        "PRAGMA journal_mode=WAL2",
        "SELECT 'PRAGMA journal_mode=WAL'",
    ],
)
def test_w002_positive(init_command):
    options = {"transaction_mode": "IMMEDIATE"}
    if init_command is not None:
        options["init_command"] = init_command
    assert run_checks({"default": raw(**options)}) == [("sqlite_ops.W002", "default")]


@pytest.mark.parametrize(
    "name", [":memory:", "file::memory:?cache=shared", "file:memdb1?mode=memory&cache=shared"]
)
def test_w002_skipped_for_memory_db(name):
    assert run_checks({"default": raw(name)}) == [("sqlite_ops.W001", "default")]


# W003: Litestream VFS + CONN_MAX_AGE (WSGI)


def test_w003_positive_default_conn_max_age():
    db = sqlite_database(VFS)
    assert run_checks({"default": db}, deploy=False) == [("sqlite_ops.W003", "default")]


def test_w003_positive_explicit_zero_and_deploy():
    db = {**sqlite_database(VFS), "CONN_MAX_AGE": 0}
    assert run_checks({"default": db}, WSGI_APPLICATION="proj.wsgi.application") == [
        ("sqlite_ops.W003", "default")
    ]


def test_w003_negative_conn_max_age_none():
    db = {**sqlite_database(VFS), "CONN_MAX_AGE": None}
    assert run_checks({"default": db}) == []


def test_w003_negative_asgi():
    # ASGI 의 영속 연결은 미검증이라 내지 않는다 (교차 리뷰)
    db = sqlite_database(VFS)
    assert run_checks({"default": db}, ASGI_APPLICATION="proj.asgi.application") == []


@pytest.mark.parametrize(
    "name", ["/srv/app/app.sqlite3", "file:/srv/app/app.sqlite3?mode=ro", "/srv/vfs=litestream"]
)
def test_w003_negative_not_vfs(name):
    assert run_checks({"default": sqlite_database(name)}) == []


# 대상 고르기·여러 별칭


def test_non_sqlite_engines_skipped():
    databases = {
        "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "app"},
        "other": {"ENGINE": "myproject.backends.sqlite3", "NAME": "/srv/x.sqlite3"},
    }
    assert run_checks(databases) == []


def test_multiple_aliases():
    databases = {
        "default": sqlite_database("/srv/app/app.sqlite3"),
        "events": raw("/srv/app/events.sqlite3", init_command=WAL),
        "legacy": raw("/srv/app/legacy.sqlite3", transaction_mode="IMMEDIATE"),
        "replica": sqlite_database(VFS),
        "pg": {"ENGINE": "django.db.backends.postgresql", "NAME": "app"},
    }
    assert run_checks(databases) == [
        ("sqlite_ops.W001", "events"),
        ("sqlite_ops.W002", "legacy"),
        ("sqlite_ops.W003", "replica"),
    ]


def test_dj_lite_style_dict_checked_against_same_table():
    # dj-lite 식으로 직접 쓴 OPTIONS 도 같은 기준이다
    db = raw(
        transaction_mode="IMMEDIATE",
        init_command="PRAGMA journal_mode=WAL;PRAGMA synchronous=NORMAL;PRAGMA busy_timeout=5000",
        timeout=20,
    )
    assert run_checks({"default": db}) == []


# E001: 프로필


@pytest.mark.parametrize(
    "sqlite_ops", [{"PROFILE": "multi-server"}, {"PROFILE": ["single-server"]}, "single-server"]
)
@pytest.mark.parametrize("deploy", [True, False])
def test_e001_only_error(sqlite_ops, deploy):
    databases = {"default": raw(), "replica": sqlite_database(VFS)}
    assert run_checks(databases, deploy=deploy, SQLITE_OPS=sqlite_ops) == [
        ("sqlite_ops.E001", None)
    ]


# 메시지 형태


MESSAGES_SCRIPT = """
import django
from django.conf import settings

from django_sqlite_ops.database import sqlite_database

settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"],
    DATABASES={
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "/srv/app/app.sqlite3"},
        "replica": sqlite_database("file:/srv/app/app.sqlite3?vfs=litestream"),
    },
)
django.setup()

from django.core.checks import run_checks

for m in run_checks(include_deployment_checks=True):
    if m.id.startswith("sqlite_ops."):
        print(m.id, m.obj, m.level, "|", m.msg, "|", m.hint)
"""


def test_messages_have_hint_and_alias():
    result = subprocess.run(
        [sys.executable, "-c", MESSAGES_SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert [line.split()[:3] for line in lines] == [
        ["sqlite_ops.W003", "replica", "30"],
        ["sqlite_ops.W001", "default", "30"],
        ["sqlite_ops.W002", "default", "30"],
    ]
    for line in lines:
        _, msg, hint = line.split(" | ")
        assert msg and "DESIGN §6" in hint
    assert "1,008ms" in lines[0] and "1.7ms" in lines[0] and "CONN_MAX_AGE" in lines[0]
    assert '"IMMEDIATE"' in lines[1] and "sqlite_database()" in lines[1]
    assert "journal_mode=WAL" in lines[2] and "sqlite_database()" in lines[2]


# DB 를 열지 않는다


NO_CONNECT_SCRIPT = """
import io
import sys
from pathlib import Path

import django
from django.conf import settings

from django_sqlite_ops.database import sqlite_database

base = Path(sys.argv[1])
names = {
    "default": base / "missing" / "app.sqlite3",
    "raw": base / "raw.sqlite3",
    "replica": f"file:{base / 'replica.sqlite3'}?vfs=litestream",
}
settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"],
    DATABASES={
        "default": sqlite_database(names["default"]),
        "raw": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(names["raw"])},
        "replica": sqlite_database(names["replica"]),
    },
    SECRET_KEY="x" * 64,
)
django.setup()

from django.core.management import call_command
from django.db import connections

assert all(connections[alias].connection is None for alias in names)
out, err = io.StringIO(), io.StringIO()
call_command("check", "--deploy", stdout=out, stderr=err)
assert all(connections[alias].connection is None for alias in names)
print(sorted(p.name for p in base.rglob("*")))
print(err.getvalue())
"""


def test_check_deploy_does_not_open_database(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", NO_CONNECT_SCRIPT, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    files, _, report = result.stdout.partition("\n")
    assert files == "[]"  # DB 파일·디렉터리가 생기지 않았다
    for expected in ("(sqlite_ops.W001) ", "(sqlite_ops.W002) ", "(sqlite_ops.W003) "):
        assert expected in report
    assert "?: (sqlite_ops." not in report  # obj 는 별칭 이름이다
    assert "raw: (sqlite_ops.W001)" in report


def test_import_package_and_database_without_django():
    # settings.py 에서 불리는 모듈들은 체크가 생긴 뒤에도 Django 를 import 하지 않는다
    assert_runs_without_django(
        """
        import django_sqlite_ops
        from django_sqlite_ops.database import sqlite_database

        sqlite_database("app.sqlite3")
        """
    )
