from django.apps import AppConfig


class SqliteOpsConfig(AppConfig):
    name = "django_sqlite_ops"
    label = "sqlite_ops"
    verbose_name = "SQLite ops"

    def ready(self) -> None:
        # 체크 등록만 한다. DB 를 열지 않는다 (DESIGN §5).
        from . import checks

        checks.register()
