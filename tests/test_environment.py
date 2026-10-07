import os
import sqlite3
import sys

import django

import django_sqlite_ops


def test_package_imports():
    assert django_sqlite_ops.__version__


def test_runtime_meets_support_floor():
    # DESIGN §5: Python 3.13+, Django 5.2+, SQLite 3.37+
    assert sys.version_info >= (3, 13)
    assert django.VERSION >= (5, 2)
    assert sqlite3.sqlite_version_info >= (3, 37)


def test_expected_sqlite_line():
    # CI 의 sqlite-floor 잡이 정말 하한 버전으로 돌았는지 확인한다
    expected = os.environ.get("EXPECT_SQLITE")
    if expected:
        assert sqlite3.sqlite_version.startswith(expected + "."), sqlite3.sqlite_version


def test_report_versions(capsys):
    with capsys.disabled():
        print(
            f"\n  python {sys.version.split()[0]} · django {django.get_version()}"
            f" · sqlite {sqlite3.sqlite_version}"
        )
