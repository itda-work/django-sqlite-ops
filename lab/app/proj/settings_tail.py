# --- lab 추가분 (lab/app/proj/settings_tail.py) -------------------------------------------
# 위는 배포 프로필 문서의 settings.py 조각 그대로다. 아래는 랩이 돌기 위한 값만 더한다.
import json  # noqa: E402
import os  # noqa: E402

SECRET_KEY = "lab-only-not-a-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "proj.urls"
USE_TZ = True
INSTALLED_APPS += ["notes"]  # noqa: F821

# 헬스 주기·grace 를 랩 시간에 맞게 줄인다(L8). 기본은 15초·60초.
_health = SQLITE_OPS["HEALTH"]  # noqa: F821
_health["REFRESH"] = float(os.environ.get("LAB_HEALTH_REFRESH", "15"))
_health["BACKLOG_GRACE"] = float(os.environ.get("LAB_HEALTH_GRACE", "60"))
if os.environ.get("LAB_HEALTH_CONFIG"):
    # L8b: 헬스의 원격 조회만 다른 toxiproxy 경로로 보낸다(업로드만 끊긴 경우).
    _health["DATABASES"]["default"]["litestream_config"] = os.environ["LAB_HEALTH_CONFIG"]

# PRAGMA 벤치: 후보 PRAGMA 를 더한다(DESIGN §6-0). 값은 JSON 객체.
if os.environ.get("LAB_PRAGMAS"):
    from django_sqlite_ops.database import sqlite_database as _sqlite_database  # noqa: E402

    DATABASES["default"] = _sqlite_database(  # noqa: F821
        DATABASES["default"]["NAME"],  # noqa: F821
        profile=SQLITE_OPS["PROFILE"],  # noqa: F821
        pragmas=json.loads(os.environ["LAB_PRAGMAS"]),
    )

# PRAGMA 벤치(#26): CONN_MAX_AGE 축. "none" 이면 None(연결 유지), 숫자면 그 초.
if os.environ.get("LAB_CONN_MAX_AGE"):
    _cma = os.environ["LAB_CONN_MAX_AGE"]
    DATABASES["default"]["CONN_MAX_AGE"] = None if _cma == "none" else int(_cma)  # noqa: F821

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "django_sqlite_ops": {"handlers": ["console"], "level": "WARNING"},
        # 벤치(#26): 500 의 원인을 컨테이너 로그에 남긴다(DEBUG=False 면 기본으로 안 찍힌다).
        "django.request": {"handlers": ["console"], "level": "ERROR"},
    },
}
