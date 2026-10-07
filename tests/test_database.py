import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from django_sqlite_ops.database import PROFILES, RECOMMENDED, recommended, sqlite_database

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_OPTIONS = {
    "transaction_mode": "IMMEDIATE",
    "init_command": "PRAGMA journal_mode=WAL;PRAGMA synchronous=NORMAL;PRAGMA busy_timeout=5000",
}


@pytest.mark.parametrize("profile", ["single-server", "single-server-multiproc"])
def test_profile_snapshot(profile):
    assert sqlite_database("/data/app.sqlite3", profile=profile) == {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "/data/app.sqlite3",
        "OPTIONS": EXPECTED_OPTIONS,
    }


def test_profiles_listed():
    assert PROFILES == ("single-server", "single-server-multiproc")


def test_default_profile_and_path_name():
    assert sqlite_database(Path("/data/app.sqlite3")) == sqlite_database(
        "/data/app.sqlite3", profile="single-server"
    )


def test_recommended_returns_copy():
    rec = recommended("single-server")
    assert rec == {
        "transaction_mode": "IMMEDIATE",
        "pragmas": {"journal_mode": "WAL", "synchronous": "NORMAL", "busy_timeout": 5000},
    }
    rec["pragmas"]["busy_timeout"] = 1
    assert recommended("single-server")["pragmas"]["busy_timeout"] == 5000


def test_table_is_read_only():
    with pytest.raises(TypeError):
        RECOMMENDED["single-server"]["pragmas"]["busy_timeout"] = 1  # type: ignore[index]


def test_pragmas_override_add_and_remove():
    db = sqlite_database(
        "x.sqlite3",
        pragmas={"synchronous": "FULL", "temp_store": "MEMORY", "busy_timeout": None},
    )
    assert db["OPTIONS"]["init_command"] == (
        "PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;PRAGMA temp_store=MEMORY"
    )


def test_pragma_value_forms():
    db = sqlite_database(
        "x.sqlite3", pragmas={"cache_size": -2000, "recursive_triggers": True, "x_flag": False}
    )
    assert db["OPTIONS"]["init_command"].endswith(
        "PRAGMA cache_size=-2000;PRAGMA recursive_triggers=ON;PRAGMA x_flag=OFF"
    )


def test_removing_all_pragmas_drops_init_command():
    db = sqlite_database(
        "x.sqlite3", pragmas={"journal_mode": None, "synchronous": None, "busy_timeout": None}
    )
    assert db["OPTIONS"] == {"transaction_mode": "IMMEDIATE"}


def test_removing_unknown_pragma_is_noop():
    assert sqlite_database("x.sqlite3", pragmas={"temp_store": None}) == sqlite_database(
        "x.sqlite3"
    )


def test_options_merge_and_override():
    db = sqlite_database("x.sqlite3", options={"timeout": 20, "transaction_mode": "EXCLUSIVE"})
    assert db["OPTIONS"] == {
        "transaction_mode": "EXCLUSIVE",
        "init_command": EXPECTED_OPTIONS["init_command"],
        "timeout": 20,
    }


def test_options_init_command_rejected():
    with pytest.raises(ValueError, match=r"pragmas="):
        sqlite_database("x.sqlite3", options={"init_command": "PRAGMA foo=1"})


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="unknown profile"):
        sqlite_database("x.sqlite3", profile="multi-server")
    with pytest.raises(ValueError, match="unknown profile"):
        recommended("nope")


@pytest.mark.parametrize(
    "name", ["journal-mode", "1abc", "", "a;DROP TABLE t", "a b", "main.journal_mode", 3]
)
def test_invalid_pragma_name_rejected(name):
    with pytest.raises(ValueError, match="invalid PRAGMA name"):
        sqlite_database("x.sqlite3", pragmas={name: 1})


def test_invalid_pragma_name_rejected_even_when_removing():
    with pytest.raises(ValueError, match="invalid PRAGMA name"):
        sqlite_database("x.sqlite3", pragmas={"x;y": None})


@pytest.mark.parametrize("value", ["WAL; DROP TABLE t", "'x'", "a b", "", 1.5, b"WAL", [1]])
def test_invalid_pragma_value_rejected(value):
    with pytest.raises(ValueError, match="invalid value"):
        sqlite_database("x.sqlite3", pragmas={"journal_mode": value})


def test_import_without_django():
    # settings.py 에서 부르는 모듈이라 Django 없이 import 되어야 한다 (DESIGN §6-0)
    code = textwrap.dedent(
        """
        import sys

        class BlockDjango:
            def find_spec(self, name, path=None, target=None):
                if name == "django" or name.startswith("django."):
                    raise ImportError(f"blocked: {name}")
                return None

        sys.meta_path.insert(0, BlockDjango())
        from django_sqlite_ops.database import sqlite_database

        sqlite_database("app.sqlite3")
        leaked = sorted(m for m in sys.modules if m == "django" or m.startswith("django."))
        assert not leaked, leaked
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def _design_default_on_rows():
    text = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    section = text.split("### 6-0.", 1)[1].split("\n### ", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[3] == "켬":
            names = re.findall(r"`([^`]+)`", cells[0])
            assert len(names) == 1, line
            rows[names[0]] = cells[1].strip("`")
    return rows


@pytest.mark.parametrize("profile", PROFILES)
def test_defaults_match_design_table(profile):
    # DESIGN §6-0 표에서 "기본 적용 = 켬"인 항목과 코드의 기본값이 같아야 한다
    rows = _design_default_on_rows()
    rec = recommended(profile)
    code = {"transaction_mode": rec["transaction_mode"], **rec["pragmas"]}
    assert set(rows) == set(code)
    for name, doc_value in rows.items():
        value = code[name]
        if isinstance(value, int):
            assert doc_value == f"{value}ms", (name, doc_value)
        else:
            assert doc_value == value, (name, doc_value)


DJANGO_CONNECTION_SCRIPT = """
import sqlite3
import sys

import django
from django.conf import settings

from django_sqlite_ops.database import sqlite_database

path = sys.argv[1]
settings.configure(DATABASES={"default": sqlite_database(path)}, USE_TZ=True)
django.setup()

from django.db import connection, transaction

with connection.cursor() as cursor:
    for pragma in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys"):
        cursor.execute(f"PRAGMA {pragma}")
        print(pragma, cursor.fetchone()[0])
    cursor.execute("CREATE TABLE t (x INTEGER)")

# IMMEDIATE 이면 쓰기 전이라도 atomic() 진입 시점에 쓰기 잠금(RESERVED)을 잡는다.
other = sqlite3.connect(path, timeout=0, isolation_level=None)
with transaction.atomic():
    try:
        other.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        print("other_in_atomic", exc)
    else:
        print("other_in_atomic acquired")
        other.execute("ROLLBACK")
other.execute("BEGIN IMMEDIATE")
other.execute("ROLLBACK")
print("other_after_atomic acquired")
"""


def test_django_connection_applies_settings(tmp_path):
    path = tmp_path / "app.sqlite3"
    result = subprocess.run(
        [sys.executable, "-c", DJANGO_CONNECTION_SCRIPT, str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "journal_mode wal",
        "synchronous 1",  # NORMAL
        "busy_timeout 5000",
        "foreign_keys 1",  # Django 가 켠다. 우리는 건드리지 않는다
        "other_in_atomic database is locked",
        "other_after_atomic acquired",
    ]


def test_without_transaction_mode_atomic_is_deferred(tmp_path):
    # 대조군: transaction_mode 가 없으면(DEFERRED) atomic() 진입만으로는 잠그지 않는다
    script = DJANGO_CONNECTION_SCRIPT.replace(
        "sqlite_database(path)", 'sqlite_database(path, options={"transaction_mode": None})'
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "app.sqlite3")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "other_in_atomic acquired" in result.stdout.splitlines()
