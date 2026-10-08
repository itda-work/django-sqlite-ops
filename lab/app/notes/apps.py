from django.apps import AppConfig


class NotesConfig(AppConfig):
    name = "notes"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # 벤치 계측(#26): 연결 생성 횟수를 센다. DB 는 열지 않는다.
        from django.db.backends.signals import connection_created

        from . import metrics

        connection_created.connect(metrics.on_connection_created, dispatch_uid="lab-conn-count")
