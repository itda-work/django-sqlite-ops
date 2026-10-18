"""``manage.py sqlite_doctor`` — 실제 연결로 SQLite 운영 상태를 진단한다 (DESIGN §6-2).

로직은 ``django_sqlite_ops.doctor``(일반 진단)와 ``django_sqlite_ops.restore_check``
(``--restore-test``)에 있다. 이 명령은 옵션을 받아 출력하고 종료 코드를 정한다.
"""

import math
import sys

from django.core.management.base import BaseCommand

from ...boot.litestream import DEFAULT_RESTORE_TIMEOUT
from ...doctor import diagnose, exit_code, format_text, to_json
from ...restore_check import DEFAULT_INTEGRITY_TIMEOUT, INTEGRITY_MODES, restore_test

HELP = (
    "Diagnose SQLite databases with a real connection: actual PRAGMA values after "
    "init_command, file and WAL sizes, filesystem type, Litestream config paths and the "
    "channel layer. Connecting runs OPTIONS['init_command'], which can change the database "
    "(PRAGMA journal_mode=WAL persists in the file), and connecting to a WAL database can "
    "create -wal/-shm files. A database whose file does not exist is reported and NOT "
    "connected (connecting would create an empty file); neither is a read-only alias of a "
    "WAL database (diagnose it through the writer's alias or an immutable=1 URI). "
    "With --restore-test it does only one thing instead: restore each write database from its "
    "Litestream replica into a new directory under --restore-dir, check the TXID and run "
    "PRAGMA integrity_check on the copy (without --database, only the write file databases; other "
    "sqlite3 aliases are reported as skipped, and having none to test is an error). That mode "
    "opens no Django connection. It downloads the "
    "whole snapshot (time and S3 egress grow with the database). It refuses a --restore-dir that "
    "overlaps a database directory or a litestream meta directory it knows of for any sqlite3 "
    "alias "
    "(SQLITE_OPS['HEALTH'] meta_path or the default .<db>-litestream); a YAML meta-path outside "
    "the database directory that is not in SQLITE_OPS['HEALTH'] is not protected. "
    "Exit status: 0 no problems, 1 warnings or undeterminable items, 2 errors "
    "(including invalid --restore-test arguments)."
)

# --restore-test 에만 의미가 있는 옵션(dest → 옵션 이름).
_RESTORE_ONLY = {
    "restore_dir": "--restore-dir",
    "integrity": "--integrity",
    "restore_timeout": "--restore-timeout",
    "integrity_timeout": "--integrity-timeout",
    "keep": "--keep",
}


def _usage_error(message: str) -> None:
    """이 모드의 인자 오류는 exit 2 다(D-20). ``CommandError`` 의 기본 코드 1 은 쓰지 않는다."""
    print(f"sqlite_doctor: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _seconds(option: str, value: object) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        _usage_error(f"{option} must be a number of seconds, got {value!r}")
    if not math.isfinite(seconds) or seconds <= 0:
        _usage_error(f"{option} must be a positive number of seconds, got {value!r}")
    return seconds


class Command(BaseCommand):
    help = HELP
    requires_system_checks: list[str] = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            action="append",
            dest="databases",
            metavar="ALIAS",
            help="Database alias to diagnose; repeatable. Default: every sqlite3 alias.",
        )
        parser.add_argument(
            "--litestream-config",
            metavar="PATH",
            help="Litestream config file to match against DATABASES. Skipped when omitted "
            "(required with --restore-test).",
        )
        parser.add_argument(
            "--litestream",
            metavar="BIN",
            default="litestream",
            help="Litestream binary (default: litestream on PATH).",
        )
        parser.add_argument("--json", action="store_true", help="Print JSON (schema version 1).")
        group = parser.add_argument_group("restore test (--restore-test)")
        group.add_argument(
            "--restore-test",
            action="store_true",
            help="Only restore each write database from its replica and verify the copy.",
        )
        group.add_argument(
            "--restore-dir",
            metavar="DIR",
            help="Existing real directory (not a symlink) for the restored copies; required. "
            "Not /tmp on a tmpfs server, not the database directory.",
        )
        group.add_argument(
            "--integrity",
            choices=INTEGRITY_MODES,
            help="PRAGMA integrity_check (full, default) or quick_check (quick).",
        )
        group.add_argument(
            "--restore-timeout",
            metavar="SEC",
            help=f"Time limit for each restore (default {DEFAULT_RESTORE_TIMEOUT:g}).",
        )
        group.add_argument(
            "--integrity-timeout",
            metavar="SEC",
            help="Separate time limit for each integrity check "
            f"(default {DEFAULT_INTEGRITY_TIMEOUT:g}).",
        )
        group.add_argument(
            "--keep",
            action="store_true",
            help="Keep the restored copies even when every check passes.",
        )

    def handle(self, *args, **options):
        if options["restore_test"]:
            items = self._restore_test(options)
        else:
            given = [n for dest, n in _RESTORE_ONLY.items() if options[dest] not in (None, False)]
            if given:
                _usage_error(f"{', '.join(given)} require(s) --restore-test")
            items = diagnose(
                options["databases"],
                litestream_config=options["litestream_config"],
                litestream_binary=options["litestream"],
            )
        self.stdout.write(to_json(items) if options["json"] else format_text(items))
        code = exit_code(items)
        if code:
            sys.exit(code)

    def _restore_test(self, options):
        if not options["restore_dir"]:
            _usage_error("--restore-test requires --restore-dir DIR (D-20)")
        if not options["litestream_config"]:
            _usage_error("--restore-test requires --litestream-config PATH")
        restore_timeout = DEFAULT_RESTORE_TIMEOUT
        if options["restore_timeout"] is not None:
            restore_timeout = _seconds("--restore-timeout", options["restore_timeout"])
        integrity_timeout = DEFAULT_INTEGRITY_TIMEOUT
        if options["integrity_timeout"] is not None:
            integrity_timeout = _seconds("--integrity-timeout", options["integrity_timeout"])
        return restore_test(
            options["databases"],
            restore_dir=options["restore_dir"],
            litestream_config=options["litestream_config"],
            litestream_binary=options["litestream"],
            integrity=options["integrity"] or "full",
            restore_timeout=restore_timeout,
            integrity_timeout=integrity_timeout,
            keep=options["keep"],
        )
