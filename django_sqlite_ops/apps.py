from django.apps import AppConfig
from django.conf import settings


class SqliteOpsConfig(AppConfig):
    name = "django_sqlite_ops"
    label = "sqlite_ops"
    verbose_name = "SQLite ops"

    def ready(self) -> None:
        # 체크 등록과 명시적으로 켠 호환 패치만 한다. DB 를 열지 않는다 (DESIGN §5·§9).
        from . import checks
        from .compat import channels_lite

        checks.register()
        if channels_lite.enabled(getattr(settings, "SQLITE_OPS", {})):
            # 게이트 밖이거나 channels-lite 가 없으면 아무것도 바꾸지 않는다.
            # 사유는 sqlite_doctor 가 알린다.
            channels_lite.apply()
