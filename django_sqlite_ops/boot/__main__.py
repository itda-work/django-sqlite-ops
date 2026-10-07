"""``python -m django_sqlite_ops.boot`` (DESIGN §4-2). Django 를 import 하지 않는다."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
