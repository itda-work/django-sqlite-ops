"""``manage.py sqlite_doctor`` — 실제 연결로 SQLite 운영 상태를 진단한다 (DESIGN §6-2).

로직은 ``django_sqlite_ops.doctor`` 에 있다. 이 명령은 옵션을 받아 출력하고 종료 코드를 정한다.
"""

import sys

from django.core.management.base import BaseCommand

from ...doctor import diagnose, exit_code, format_text, to_json

HELP = (
    "Diagnose SQLite databases with a real connection: actual PRAGMA values after "
    "init_command, file and WAL sizes, filesystem type, Litestream config paths and the "
    "channel layer. Connecting runs OPTIONS['init_command'], which can change the database "
    "(PRAGMA journal_mode=WAL persists in the file), and connecting to a WAL database can "
    "create -wal/-shm files. A database whose file does not exist is reported and NOT "
    "connected (connecting would create an empty file); neither is a read-only alias of a "
    "WAL database (diagnose it through the writer's alias or an immutable=1 URI). "
    "Exit status: 0 no problems, 1 warnings or undeterminable items, 2 errors."
)


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
            help="Litestream config file to match against DATABASES. Skipped when omitted.",
        )
        parser.add_argument(
            "--litestream",
            metavar="BIN",
            default="litestream",
            help="Litestream binary (default: litestream on PATH).",
        )
        parser.add_argument("--json", action="store_true", help="Print JSON (schema version 1).")

    def handle(self, *args, **options):
        items = diagnose(
            options["databases"],
            litestream_config=options["litestream_config"],
            litestream_binary=options["litestream"],
        )
        self.stdout.write(to_json(items) if options["json"] else format_text(items))
        code = exit_code(items)
        if code:
            sys.exit(code)
