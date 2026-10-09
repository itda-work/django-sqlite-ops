"""정적 시스템 체크 W001–W005·E001 (DESIGN §6-1).

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


@pytest.mark.parametrize(
    "evidence",
    [
        {"ASGI_APPLICATION": "proj.asgi.application"},
        {"SQLITE_OPS": {"PROFILE": "single-server"}},
    ],
)
def test_w003_negative_asgi(evidence):
    # ASGI 의 영속 연결은 미검증이라 내지 않는다 (교차 리뷰). ASGI 판단은 W005 와 같은 규칙이다
    db = sqlite_database(VFS)
    assert run_checks({"default": db}, **evidence) == []


@pytest.mark.parametrize(
    "name", ["/srv/app/app.sqlite3", "file:/srv/app/app.sqlite3?mode=ro", "/srv/vfs=litestream"]
)
def test_w003_negative_not_vfs(name):
    assert run_checks({"default": sqlite_database(name)}) == []


# W005: 일반 별칭 + CONN_MAX_AGE=None (ASGI)

# ASGI 판단 근거(DESIGN §6-1 표). 값: 설정, ASGI 로 판단하는가
EVIDENCE = {
    "none": ({}, False),
    "wsgi_application": ({"WSGI_APPLICATION": "proj.wsgi.application"}, False),
    "asgi_application": ({"ASGI_APPLICATION": "proj.asgi.application"}, True),
    "asgi_application_empty": ({"ASGI_APPLICATION": ""}, False),
    "profile_single": ({"SQLITE_OPS": {"PROFILE": "single-server"}}, True),
    "profile_multiproc": ({"SQLITE_OPS": {"PROFILE": "single-server-multiproc"}}, True),
    "sqlite_ops_without_profile": ({"SQLITE_OPS": {}}, False),  # 기본 프로필은 근거가 아니다
    "both": (
        {
            "ASGI_APPLICATION": "proj.asgi.application",
            "WSGI_APPLICATION": "proj.wsgi.application",
            "SQLITE_OPS": {"PROFILE": "single-server"},
        },
        True,
    ),
}
# 별칭 종류: NAME, VFS 별칭인가
KINDS = {
    "default": ("/srv/app/app.sqlite3", False),  # 일반 쓰기 별칭
    "readonly": ("file:/srv/app/app.sqlite3?mode=ro", False),
    "memory": (":memory:", False),
    "vfs": (VFS, True),
}
MISSING = object()
CONN_MAX_AGES = {"missing": MISSING, "zero": 0, "none": None, "positive": 60}


@pytest.mark.parametrize("cma", CONN_MAX_AGES)
@pytest.mark.parametrize("evidence", EVIDENCE)
def test_w003_w005_table(evidence, cma):
    # 종류마다 별칭 하나씩 한 설정에 넣는다. 한 설정의 ASGI 판단은 하나라 W003·W005 가
    # 함께 나지 않는다.
    # W003: WSGI 판단 · VFS · None 아님. W005: ASGI 판단 · VFS 아님 · None
    extra, asgi = EVIDENCE[evidence]
    value = CONN_MAX_AGES[cma]
    databases = {}
    for kind, (name, _) in KINDS.items():
        db = sqlite_database(name)
        if value is not MISSING:
            db["CONN_MAX_AGE"] = value
        databases[kind] = db
    expected = []
    for kind, (_, vfs) in KINDS.items():
        if not asgi and vfs and value is not None:
            expected.append(("sqlite_ops.W003", kind))
        if asgi and not vfs and value is None:
            expected.append(("sqlite_ops.W005", kind))
    assert run_checks(databases, deploy=False, **extra) == sorted(expected)


@pytest.mark.parametrize("evidence", EVIDENCE)
def test_w003_w005_never_disagree_in_one_settings(evidence):
    # 한 설정에 W003 대상(VFS·0)과 W005 대상(일반·None)을 함께 둔다. 판단이 하나라 둘 중 하나만 난다
    extra, asgi = EVIDENCE[evidence]
    databases = {
        "default": {**sqlite_database("/srv/app/app.sqlite3"), "CONN_MAX_AGE": None},
        "replica": {**sqlite_database(VFS), "CONN_MAX_AGE": 0},
    }
    expected = [("sqlite_ops.W005", "default")] if asgi else [("sqlite_ops.W003", "replica")]
    assert run_checks(databases, **extra) == expected


def test_w005_skips_non_sqlite_engines():
    databases = {
        "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "app", "CONN_MAX_AGE": None},
        "other": {
            "ENGINE": "myproject.backends.sqlite3",
            "NAME": "/srv/x.sqlite3",
            "CONN_MAX_AGE": None,
        },
    }
    assert run_checks(databases, ASGI_APPLICATION="proj.asgi.application") == []


SILENCED_SCRIPT = """
import io
import json
import sys

import django
from django.conf import settings

from django_sqlite_ops.database import sqlite_database

db = {**sqlite_database("/srv/app/app.sqlite3"), "CONN_MAX_AGE": None}
settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"],
    DATABASES={"default": db},
    ASGI_APPLICATION="proj.asgi.application",
    SILENCED_SYSTEM_CHECKS=json.loads(sys.argv[1]),
)
django.setup()

from django.core.management import call_command

err = io.StringIO()
call_command("check", stderr=err)
print(err.getvalue())
"""


@pytest.mark.parametrize("silenced", [[], ["sqlite_ops.W005"]])
def test_w005_silenced(silenced):
    # SILENCED_SYSTEM_CHECKS 는 run_checks() 가 아니라 check 명령이 거른다
    result = subprocess.run(
        [sys.executable, "-c", SILENCED_SCRIPT, json.dumps(silenced)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert ("default: (sqlite_ops.W005)" in result.stdout) == (not silenced)


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"ASGI_APPLICATION": "proj.asgi.application"}, "ASGI_APPLICATION is set"),
        (
            {"SQLITE_OPS": {"PROFILE": "single-server-multiproc"}},
            "SQLITE_OPS['PROFILE'] = 'single-server-multiproc' is an ASGI deployment profile",
        ),
    ],
)
def test_w005_message(extra, reason):
    result = subprocess.run(
        [sys.executable, "-c", W005_MESSAGE_SCRIPT, json.dumps(extra)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    ident, obj, level, msg, hint = result.stdout.rstrip("\n").split("\n")
    assert (ident, obj, level) == ("sqlite_ops.W005", "default", "30")
    assert msg == f"Database 'default' has CONN_MAX_AGE = None under ASGI ({reason})."
    for part in (
        "not reused",
        "810-982",
        "suspected cause",
        'DATABASES["default"]["CONN_MAX_AGE"] = 0 or remove the key',
        "docs/research/bench-2026-10-08.md",
        "DESIGN §6-1",
        "silence sqlite_ops.W005",
    ):
        assert part in hint


W005_MESSAGE_SCRIPT = """
import json
import sys

import django
from django.conf import settings

from django_sqlite_ops.database import sqlite_database

db = {**sqlite_database("/srv/app/app.sqlite3"), "CONN_MAX_AGE": None}
settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"], DATABASES={"default": db}, **json.loads(sys.argv[1])
)
django.setup()

from django.core.checks import run_checks

for m in run_checks():
    if m.id.startswith("sqlite_ops."):
        print(m.id, m.obj, m.level, m.msg, m.hint, sep="\\n")
"""


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
if sys.argv[2:] == ["asgi"]:
    # W005 경로도 DB 를 열지 않는다
    settings.ASGI_APPLICATION = "proj.asgi.application"
    settings.DATABASES["raw"]["CONN_MAX_AGE"] = None
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


@pytest.mark.parametrize(
    ("server", "conn_max_age_id"), [("wsgi", "sqlite_ops.W003"), ("asgi", "sqlite_ops.W005")]
)
def test_check_deploy_does_not_open_database(tmp_path, server, conn_max_age_id):
    result = subprocess.run(
        [sys.executable, "-c", NO_CONNECT_SCRIPT, str(tmp_path), server],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    files, _, report = result.stdout.partition("\n")
    assert files == "[]"  # DB 파일·디렉터리가 생기지 않았다
    for expected in ("(sqlite_ops.W001) ", "(sqlite_ops.W002) ", f"({conn_max_age_id}) "):
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


# 실제 연결 대조 (리뷰 1): 체크 결과와 Django 가 실제로 연 연결의 journal_mode 가 어긋나지 않는다.
# 별칭 하나가 표 한 행이다. 체크를 먼저 돌리고(DB 를 열지 않음), 그 뒤에 별칭마다 연결해 본다.

ACTUAL_SCRIPT = """
import json
import sqlite3
import sys
from pathlib import Path

import django
from django.conf import settings

base = Path(sys.argv[1])
rows = json.loads(sys.argv[2])
databases = {}
for alias, row in rows.items():
    if row.get("create"):
        with sqlite3.connect(base / row["create"]) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
        conn.close()
    databases[alias] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": (Path if row.get("pathlike") else str)(row["name"].format(base=base)),
        "OPTIONS": row["options"],
    }
# Django 는 default 별칭을 요구한다. sqlite 가 아닌 엔진이라 체크 대상이 아니다
databases["default"] = {"ENGINE": "django.db.backends.dummy"}
settings.configure(INSTALLED_APPS=["django_sqlite_ops"], DATABASES=databases)
django.setup()

from django.core.checks import run_checks
from django.db import connections

result = {alias: {"ids": [], "actual": None} for alias in rows}
for m in run_checks(include_deployment_checks=True):
    if m.id.startswith("sqlite_ops."):
        result[m.obj]["ids"].append(m.id)
for alias in rows:
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            result[alias]["actual"] = cursor.fetchone()[0]
    except Exception as exc:
        result[alias]["actual"] = f"error: {exc}"
    connections[alias].close()
print(json.dumps(result))
"""


def run_actual(rows, tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", ACTUAL_SCRIPT, str(tmp_path), json.dumps(rows)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# (init_command, W002 를 내는가, 실제 journal_mode)
INIT_COMMAND_TABLE = [
    ("PRAGMA journal_mode=WAL", False, "wal"),
    ("PRAGMA journal_mode = wal", False, "wal"),
    ("PRAGMA main.journal_mode=WAL", False, "wal"),
    ('PRAGMA "journal_mode"=WAL', False, "wal"),
    ("PRAGMA [journal_mode]=WAL", False, "wal"),
    ("PRAGMA `journal_mode`=WAL", False, "wal"),
    ('PRAGMA "main"."journal_mode"=WAL', False, "wal"),
    ("PRAGMA journal_mode('wal')", False, "wal"),
    ("PRAGMA journal_mode=WAL -- enable WAL", False, "wal"),
    ("/* wal */ PRAGMA journal_mode=WAL", False, "wal"),
    ("PRAGMA journal_mode=/* x */WAL", False, "wal"),
    ("PRAGMA journal_mode=DELETE;PRAGMA journal_mode=WAL", False, "wal"),
    ("PRAGMA journal_mode=WAL;PRAGMA temp.journal_mode=DELETE", False, "wal"),
    ("PRAGMA journal_mode=WAL;PRAGMA journal_mode", False, "wal"),
    ("PRAGMA journal_mode=WAL; PRAGMA journal_mode=DELETE -- rollback journal", True, "delete"),
    ('PRAGMA journal_mode=WAL; PRAGMA "journal_mode"=DELETE', True, "delete"),
    ("PRAGMA journal_mode=WAL; PRAGMA [journal_mode]=DELETE", True, "delete"),
    ("PRAGMA journal_mode=WAL; PRAGMA journal_mode/* x */=DELETE", True, "delete"),
    ("PRAGMA journal_mode=WAL;PRAGMA journal_mode=DELETE", True, "delete"),
    ("PRAGMA temp.journal_mode=WAL", True, "delete"),
    ("PRAGMA journal_mode=DELETE -- PRAGMA journal_mode=WAL", True, "delete"),
    ("PRAGMA synchronous=NORMAL", True, "delete"),
]


def test_w002_matches_real_connection(tmp_path):
    rows = {
        f"db{i}": {
            "name": f"{{base}}/db{i}.sqlite3",
            "options": {"transaction_mode": "IMMEDIATE", "init_command": init_command},
        }
        for i, (init_command, _, _) in enumerate(INIT_COMMAND_TABLE)
    }
    result = run_actual(rows, tmp_path)
    got = [
        (cmd, result[f"db{i}"]["ids"] == ["sqlite_ops.W002"], result[f"db{i}"]["actual"])
        for i, (cmd, _, _) in enumerate(INIT_COMMAND_TABLE)
    ]
    assert got == INIT_COMMAND_TABLE
    for i, (_, warned, actual) in enumerate(got):
        assert warned or actual == "wal", INIT_COMMAND_TABLE[i]
        if not warned:
            assert result[f"db{i}"]["ids"] == []


@pytest.mark.parametrize(
    "init_command",
    [
        # journal_mode 를 언급하지만 형식을 확정할 수 없다 → 판정할 수 없음으로 W002
        "PRAGMA journal_mode=WAL;PRAGMA journal_mode = WAL WAL",
        "PRAGMA journal_mode=WAL;PRAGMA journal_mode=",
        "PRAGMA journal_mode=WAL;PRAGMA main..journal_mode=DELETE",
    ],
)
def test_w002_undeterminable_statement_warns(init_command):
    db = raw(transaction_mode="IMMEDIATE", init_command=init_command)
    assert run_checks({"default": db}) == [("sqlite_ops.W002", "default")]


UNDETERMINABLE_SCRIPT = """
import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "/srv/app/app.sqlite3",
            "OPTIONS": {
                "transaction_mode": "IMMEDIATE",
                "init_command": "PRAGMA journal_mode=WAL;PRAGMA journal_mode = WAL WAL",
            },
        }
    },
)
django.setup()

from django.core.checks import run_checks

for m in run_checks(include_deployment_checks=True):
    if m.id.startswith("sqlite_ops."):
        print(m.id, "|", m.msg, "|", m.hint)
"""


def test_w002_undeterminable_message():
    result = subprocess.run(
        [sys.executable, "-c", UNDETERMINABLE_SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (line,) = result.stdout.splitlines()
    ident, msg, hint = line.split(" | ")
    assert ident == "sqlite_ops.W002"
    assert "cannot determine" in msg and "PRAGMA journal_mode = WAL WAL" in msg
    assert "journal_mode=WAL" in hint


W001, W002, W004 = "sqlite_ops.W001", "sqlite_ops.W002", "sqlite_ops.W004"
READONLY = "error: attempt to write a readonly database"
REMEDY = {"transaction_mode": "IMMEDIATE", "init_command": "PRAGMA journal_mode=WAL"}

# 별칭 역할 판별 표 (리뷰 1·2). 행마다:
#   (NAME, 미리 만들 파일, PathLike 로 줄지,
#    OPTIONS 없이: (체크, 실제 journal_mode),
#    W001·W002 안내(REMEDY)를 적용한 뒤: (체크, 실제 journal_mode))
# 역할을 판정할 수 없으면 W004 만 내고, 안내를 적용해도 W004 는 사라지지 않는다.
NAME_TABLE = [
    # 메모리 DB: W002 를 건너뛴다
    (":memory:", None, False, ([W001], "memory"), ([], "memory")),
    (":memory:", None, True, ([W001], "memory"), ([], "memory")),
    ("file::memory:", None, False, ([W001], "memory"), ([], "memory")),
    ("file::memory:?cache=shared", None, False, ([W001], "memory"), ([], "memory")),
    ("file:memdb1?mode=memory&cache=shared", None, False, ([W001], "memory"), ([], "memory")),
    # 퍼센트 인코딩된 :memory: 도 메모리다 (파일명을 디코딩한 뒤 비교)
    ("file:%3Amemory%3A", None, False, ([W001], "memory"), ([], "memory")),
    ("file:%3amemory%3a?cache=shared", None, False, ([W001], "memory"), ([], "memory")),
    # 메모리처럼 보이지만 실제로는 파일이다: 검사한다
    ("file::memory:backup.sqlite3", None, False, ([W001, W002], "delete"), ([], "wal")),
    # 인코딩된 구분자는 구분자가 아니다: 파일명이 "q?mode=ro.sqlite3" 인 쓰기 DB
    ("file:{base}/q%3Fmode%3Dro.sqlite3", None, False, ([W001, W002], "delete"), ([], "wal")),
    # 프래그먼트 뒤는 쿼리가 아니다
    (
        "file:{base}/frag.sqlite3?mode=rwc#?mode=ro",
        None,
        False,
        ([W001, W002], "delete"),
        ([], "wal"),
    ),
    # 읽기 전용: W001·W002 를 건너뛴다
    ("file:{base}/ro.sqlite3?mode=ro", "ro.sqlite3", False, ([], "delete"), ([], READONLY)),
    ("file:{base}/key.sqlite3?mo%64e=r%6F", "key.sqlite3", False, ([], "delete"), ([], READONLY)),
    ("file:{base}/i1.sqlite3?immutable=1", "i1.sqlite3", False, ([], "delete"), ([], "delete")),
    ("file:{base}/i2.sqlite3?immutable=true", "i2.sqlite3", False, ([], "delete"), ([], "delete")),
    ("file:{base}/i3.sqlite3?immutable=yes", "i3.sqlite3", False, ([], "delete"), ([], "delete")),
    ("file:{base}/i4.sqlite3?immutable=On", "i4.sqlite3", False, ([], "delete"), ([], "delete")),
    # immutable 거짓값은 쓰기 DB 다
    (
        "file:{base}/i5.sqlite3?immutable=0",
        "i5.sqlite3",
        False,
        ([W001, W002], "delete"),
        ([], "wal"),
    ),
    (
        "file:{base}/i6.sqlite3?immutable=OFF",
        "i6.sqlite3",
        False,
        ([W001, W002], "delete"),
        ([], "wal"),
    ),
    # 역할을 판정할 수 없다: W004. SQLite 의 중복·비표준 해석을 흉내 내지 않는다
    (
        "file:{base}/regular.sqlite3?mode=memory&mode=rwc",
        None,
        False,
        ([W004], "delete"),
        ([W004], "wal"),
    ),
    ("file:memory?mode=memory&mode=memory", None, False, ([W004], "memory"), ([W004], "memory")),
    (
        "file:{base}/d1.sqlite3?mode=rwc&mode=ro",
        "d1.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], READONLY),
    ),
    (
        "file:{base}/d2.sqlite3?mode=ro&mode=ro",
        "d2.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], READONLY),
    ),
    (
        "file:{base}/d3.sqlite3?mode=ro&mode=rwc",
        "d3.sqlite3",
        False,
        ([W004], "error: access mode not allowed: rwc"),
        ([W004], "error: access mode not allowed: rwc"),
    ),
    (
        "file:{base}/d4.sqlite3?mode=ro%00ignored",
        "d4.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], READONLY),
    ),
    (
        "file:{base}/d5.sqlite3?mode=RO",
        "d5.sqlite3",
        False,
        ([W004], "error: no such access mode: RO"),
        ([W004], "error: no such access mode: RO"),
    ),
    (
        "file:{base}/d6.sqlite3?immutable=1&immutable=0",
        "d6.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], "delete"),
    ),
    (
        "file:{base}/d7.sqlite3?immutable=0&immutable=1",
        "d7.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], "wal"),
    ),
    (
        "file:{base}/d8.sqlite3?immutable=2",
        "d8.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], "delete"),
    ),
    # 디코딩 뒤 NUL 이 든 키·값·파일명 (리뷰 3): SQLite 는 NUL 앞까지만 읽으므로 역할 키가 숨는다
    (
        "file:{base}/n1.sqlite3?mode%00ignored=ro",
        "n1.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], READONLY),
    ),
    (
        "file:{base}/n2.sqlite3?mode%00=ro",
        "n2.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], READONLY),
    ),
    (
        "file:{base}/n3.sqlite3?immutable%00ignored=0&immutable=1",
        "n3.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], "wal"),
    ),
    (
        "file:{base}/n4.sqlite3?mode=memory&mode%00ignored=rwc",
        None,
        False,
        ([W004], "delete"),
        ([W004], "wal"),
    ),
    ("file:{base}/n5.sqlite3?cache%00x=shared", None, False, ([W004], "delete"), ([W004], "wal")),
    ("file:{base}/n6%00x.sqlite3", None, False, ([W004], "delete"), ([W004], "wal")),
    # URI 가 아닌 일반 경로의 "%00" 은 글자 그대로다: 쓰기 DB
    ("{base}/p%00.sqlite3", None, False, ([W001, W002], "delete"), ([], "wal")),
    (
        "file:{base}/d9.sqlite3?vfs=litestream&vfs=unix",
        "d9.sqlite3",
        False,
        ([W004], "delete"),
        ([W004], "wal"),
    ),
    # URI authority (라운드 #6 리뷰 1): "//" 뒤 다음 "/" 까지. 비었거나 정확히 "localhost" 만
    # 로컬이다(https://www.sqlite.org/uri.html). 대소문자를 가리고 퍼센트 디코딩하지 않는다(실측)
    (
        "file://localhost{base}/a1.sqlite3?mode=ro",
        "a1.sqlite3",
        False,
        ([], "delete"),
        ([], READONLY),
    ),
    ("file://{base}/a2.sqlite3", None, False, ([W001, W002], "delete"), ([], "wal")),
    (
        "file://example.com{base}/a3.sqlite3?mode=rwc",
        None,
        False,
        ([W004], "error: invalid uri authority: example.com"),
        ([W004], "error: invalid uri authority: example.com"),
    ),
    (
        "file://LOCALHOST{base}/a4.sqlite3?mode=ro",
        "a4.sqlite3",
        False,
        ([W004], "error: invalid uri authority: LOCALHOST"),
        ([W004], "error: invalid uri authority: LOCALHOST"),
    ),
    (
        "file://localhost?mode=memory",
        None,
        False,
        ([W004], "error: invalid uri authority: localhost?mode=memory"),
        ([W004], "error: invalid uri authority: localhost?mode=memory"),
    ),
]


def test_alias_roles_match_real_connection(tmp_path):
    rows = {}
    for i, (name, create, pathlike, _, _) in enumerate(NAME_TABLE):
        rows[f"db{i}"] = {"name": name, "create": create, "pathlike": pathlike, "options": {}}
    plain = run_actual(rows, tmp_path)
    for row in rows.values():
        row["create"] = None
        row["options"] = REMEDY
    remedied = run_actual(rows, tmp_path)
    got = [
        (
            name,
            create,
            pathlike,
            (sorted(plain[f"db{i}"]["ids"]), plain[f"db{i}"]["actual"]),
            (sorted(remedied[f"db{i}"]["ids"]), remedied[f"db{i}"]["actual"]),
        )
        for i, (name, create, pathlike, _, _) in enumerate(NAME_TABLE)
    ]
    assert got == NAME_TABLE
    # 메모리처럼 보이는 이름이 실제 파일을 만들었다 (검사 대상이어야 하는 이유)
    assert (tmp_path / ":memory:backup.sqlite3").exists()
    assert (tmp_path / "regular.sqlite3").exists()
    assert (tmp_path / "q?mode=ro.sqlite3").exists()


W004_SCRIPT = """
import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "file:/srv/app/app.sqlite3?mode=rwc&mode=ro",
        }
    },
)
django.setup()

from django.core.checks import run_checks

for deploy in (False, True):
    for m in run_checks(include_deployment_checks=deploy):
        if m.id.startswith("sqlite_ops."):
            print(deploy, m.id, m.obj, m.level, "|", m.msg, "|", m.hint)
"""


def test_w004_message_and_deploy_only():
    result = subprocess.run(
        [sys.executable, "-c", W004_SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (line,) = result.stdout.splitlines()
    head, msg, hint = line.split(" | ")
    assert head == "True sqlite_ops.W004 default 30"
    assert "cannot determine" in msg.lower() and "mode" in msg
    assert "once" in hint and "DESIGN §6-1" in hint


def test_readonly_alias_breaks_with_wal_remediation(tmp_path):
    # W002 의 안내를 읽기 전용 별칭에 적용하면 연결이 깨진다. 그래서 건너뛴다
    rows = {
        "ro": {
            "name": "file:{base}/ro.sqlite3?mode=ro",
            "create": "ro.sqlite3",
            "options": {"init_command": "PRAGMA journal_mode=WAL"},
        }
    }
    result = run_actual(rows, tmp_path)
    assert result["ro"]["ids"] == []
    assert result["ro"]["actual"] == "error: attempt to write a readonly database"


@pytest.mark.parametrize("deploy", [True, False])
def test_vfs_alias_skips_w001_w002_keeps_w003(deploy):
    databases = {"default": sqlite_database("/srv/app/app.sqlite3"), "replica": raw(VFS)}
    assert run_checks(databases, deploy=deploy) == [("sqlite_ops.W003", "replica")]
