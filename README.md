# django-sqlite-ops (가칭)

Django 에서 SQLite 를 운영 DB 로 안전하게 쓰게 하는 **운영 도구**다. 권장 설정, 부팅 시 복원 판정, 진단, 복제 헬스, 배포 프로필을 맡는다. DB 백엔드를 바꾸거나 감싸지 않고, 표준 `DATABASES` 설정만 다룬다.

## 현재 상태

| 기능 | 상태 | 이슈 |
|---|---|---|
| 권장 설정 `sqlite_database()` | 구현됨 | [#1](https://github.com/itda-work/django-sqlite-ops/issues/1) |
| boot CLI (복원 판정·복원·잠금) | 예정 | [#2](https://github.com/itda-work/django-sqlite-ops/issues/2) · [#3](https://github.com/itda-work/django-sqlite-ops/issues/3) · [#4](https://github.com/itda-work/django-sqlite-ops/issues/4) |
| 시스템 체크 | 예정 | [#5](https://github.com/itda-work/django-sqlite-ops/issues/5) |
| `sqlite_doctor` 관리 명령 | 예정 | [#6](https://github.com/itda-work/django-sqlite-ops/issues/6) |
| 복제 헬스 | 예정 | [#7](https://github.com/itda-work/django-sqlite-ops/issues/7) |
| 배포 프로필 문서 | 예정 | [#9](https://github.com/itda-work/django-sqlite-ops/issues/9) |
| channels-lite 패치 | 예정 | [#8](https://github.com/itda-work/django-sqlite-ops/issues/8) |

PyPI 배포 전이다. 패키지 이름도 가칭이라 바뀔 수 있다.

## 설치

요구 버전: Python 3.13+, Django 5.2+, SQLite 3.37+.

```bash
uv add git+https://github.com/itda-work/django-sqlite-ops
# 또는
pip install git+https://github.com/itda-work/django-sqlite-ops
```

`INSTALLED_APPS` 에 넣을 것은 아직 없다. 권장 설정은 `settings.py` 에서 함수 하나만 부른다.

## 빠른 시작

```python
# settings.py
from pathlib import Path

from django_sqlite_ops.database import sqlite_database

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASES = {
    "default": sqlite_database(BASE_DIR / "app.sqlite3"),
}
```

결과는 그대로 `DATABASES` 에 들어가는 평범한 dict 다.

```python
{
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": "/path/to/app.sqlite3",
    "OPTIONS": {
        "transaction_mode": "IMMEDIATE",
        "init_command": "PRAGMA journal_mode=WAL;PRAGMA synchronous=NORMAL;PRAGMA busy_timeout=5000",
    },
}
```

## 활용 가이드

### 권장 설정

`django_sqlite_ops.database` 는 표준 라이브러리만 import 한다. 그래서 `settings.py` 에서 불러도 Django 를 미리 불러오지 않는다.

#### 기본값

실측 근거가 있는 값만 기본으로 켠다. 근거는 [`docs/DESIGN.md`](docs/DESIGN.md) §6-0 에 있다.

| 항목 | 기본값 | 이유 |
|---|---|---|
| `transaction_mode` | `IMMEDIATE` | 트랜잭션 시작 때 쓰기 잠금을 잡는다. gunicorn 4 워커·스레드 8 로 1,200건 쓸 때 `database is locked` 0건 |
| `journal_mode` | `WAL` | 읽기와 쓰기가 서로 막지 않는다. Litestream 이 없는 개발·테스트 환경도 운영과 같게 맞춘다 |
| `synchronous` | `NORMAL` | WAL 에서 쓰기 비용을 줄인다. 체크포인트 전에 전원이 나가면 마지막 트랜잭션이 빠질 수 있다 |
| `busy_timeout` | `5000` (밀리초) | 잠금을 만나면 바로 실패하지 않고 5초 기다린다. Python `sqlite3` 의 `timeout` 기본 5초와 같은 값 |

건드리지 않는 것: `foreign_keys` 는 Django 가 이미 켠다. `wal_autocheckpoint` 는 Litestream 이 체크포인트를 관리하므로 바꾸지 않는다. `temp_store`, `mmap_size`, `cache_size`, `journal_size_limit` 는 측정 전이라 기본값이 아니다. 필요하면 `pragmas` 로 직접 켠다.

#### 프로필

`profile` 은 `"single-server"`(기본)와 `"single-server-multiproc"` 두 가지다. 지금은 두 프로필의 DB 설정이 같다. 배포 형태(워커 프로세스가 하나인지 여럿인지)에 맞는 이름을 고르면, 나중에 프로필별 값이 갈라질 때 설정을 고치지 않아도 된다. 다른 이름은 `ValueError` 다.

```python
from django_sqlite_ops.database import sqlite_database

db = sqlite_database("/srv/app/app.sqlite3", profile="single-server-multiproc")
```

#### PRAGMA 바꾸기: `pragmas`

`pragmas` 의 값으로 기본 PRAGMA 를 덮거나 새 PRAGMA 를 더한다. 값이 `None` 이면 그 PRAGMA 를 뺀다. 이름은 SQLite 처럼 대소문자를 가리지 않는다.

```python
from django_sqlite_ops.database import sqlite_database

db = sqlite_database(
    "/srv/app/app.sqlite3",
    pragmas={
        "synchronous": "FULL",  # 덮기
        "temp_store": "MEMORY",  # 더하기
        "busy_timeout": None,  # 빼기
    },
)
assert db["OPTIONS"]["init_command"] == (
    "PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;PRAGMA temp_store=MEMORY"
)
```

값은 정수, `True`/`False`(`ON`/`OFF` 로 바뀐다), 또는 `WAL`·`-2000` 같은 따옴표 없는 단어만 받는다.

#### `OPTIONS` 더하기: `options`

`options` 의 키는 `OPTIONS` 에 더해지고, 같은 키는 덮는다. `init_command` 만은 받지 않는다.

```python
from django_sqlite_ops.database import sqlite_database

db = sqlite_database("/srv/app/app.sqlite3", options={"check_same_thread": False})
assert db["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
assert db["OPTIONS"]["check_same_thread"] is False
```

#### `timeout` 과 `busy_timeout`

둘 다 잠금을 기다리는 시간이지만 단위가 다르다. `options` 의 `timeout` 은 초, PRAGMA `busy_timeout` 은 밀리초다. Django 는 연결을 연 뒤에 `init_command` 를 실행하므로 `busy_timeout` 이 이긴다. 그래서 `options={"timeout": 20}` 만 주면 대기 시간은 여전히 5000ms 다.

```python
from django_sqlite_ops.database import sqlite_database

# 대기 시간을 20초로: busy_timeout 을 덮는다 (권장)
db = sqlite_database("/srv/app/app.sqlite3", pragmas={"busy_timeout": 20000})

# timeout 을 쓰고 싶다면 busy_timeout 을 함께 뺀다
db = sqlite_database(
    "/srv/app/app.sqlite3",
    options={"timeout": 20},
    pragmas={"busy_timeout": None},
)
```

#### 거부되는 입력

판정할 수 없는 설정은 조용히 넘기지 않고 `ValueError` 로 거부한다.

| 입력 | 이유 |
|---|---|
| `options={"init_command": ...}` | 생성한 PRAGMA 와 충돌한다. PRAGMA 는 `pragmas=` 로 준다 |
| 식별자가 아닌 PRAGMA 이름 (`"journal-mode"`, `"main.journal_mode"`, `"a;b"`) | Django 는 `init_command` 를 `;` 로 나눠 문장마다 실행한다. 이름에 다른 문장이 끼어드는 것을 막는다 |
| 정수·불리언·따옴표 없는 단어가 아닌 값 (`"WAL; DROP TABLE t"`, `"'x'"`, `1.5`) | 위와 같은 이유 |
| 대소문자만 다른 같은 이름을 두 번 (`{"busy_timeout": 1, "BUSY_TIMEOUT": None}`) | 어느 쪽을 적용할지 정할 수 없다 |
| 알 수 없는 `profile` | 권장값 표에 없는 프로필이다 |

#### 여러 DB 별칭

별칭마다 따로 부른다. 파일이 다르면 잠금도 따로라서 쓰기가 많은 데이터를 다른 파일로 떼어 낼 수 있다.

```python
from pathlib import Path

from django_sqlite_ops.database import sqlite_database

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASES = {
    "default": sqlite_database(BASE_DIR / "app.sqlite3"),
    "events": sqlite_database(BASE_DIR / "events.sqlite3", pragmas={"busy_timeout": 10000}),
}
```

#### 직접 쓴 설정과 dj-lite

`sqlite_database()` 를 쓰지 않아도 된다. 직접 쓴 dict 나 dj-lite 가 만든 설정도, 나중에 들어올 시스템 체크와 `sqlite_doctor` 가 같은 권장값 표를 기준으로 검사한다(예정, [#5](https://github.com/itda-work/django-sqlite-ops/issues/5) · [#6](https://github.com/itda-work/django-sqlite-ops/issues/6)).

### boot CLI

예정. 부팅 때 Litestream 복제본과 로컬 DB 를 비교해 복원하거나, 판정할 수 없으면 기동을 거부한다. [#2](https://github.com/itda-work/django-sqlite-ops/issues/2) · [#3](https://github.com/itda-work/django-sqlite-ops/issues/3) · [#4](https://github.com/itda-work/django-sqlite-ops/issues/4)

### 시스템 체크

예정. `manage.py check` 에서 DB 를 열지 않고 설정만 권장값과 비교한다. [#5](https://github.com/itda-work/django-sqlite-ops/issues/5)

### `sqlite_doctor`

예정. DB 에 실제로 연결해 PRAGMA·마운트·Litestream 설정을 진단한다. [#6](https://github.com/itda-work/django-sqlite-ops/issues/6)

### 복제 헬스

예정. 복제 상태를 `caught_up / backlog / unknown` 으로 보고한다. [#7](https://github.com/itda-work/django-sqlite-ops/issues/7)

### 배포 프로필

예정. `single-server`, `single-server-multiproc` 의 채널 레이어·프로세스 구성 문서. [#9](https://github.com/itda-work/django-sqlite-ops/issues/9)

### channels-lite 패치

예정. channels-lite aio 중복 배달을 버전 게이트 패치로 막는다. [#8](https://github.com/itda-work/django-sqlite-ops/issues/8)

## 문제 해결

- **`options={"timeout": ...}` 을 줬는데 대기 시간이 그대로다.** `busy_timeout`(밀리초)이 `timeout`(초)보다 우선한다. 위 [`timeout` 과 `busy_timeout`](#timeout-과-busy_timeout) 을 본다.
- **DB 파일 옆에 `-wal`, `-shm` 파일이 생긴다.** WAL 모드의 정상 동작이다. 연결이 열려 있는 동안 커밋된 내용 일부가 `-wal` 에만 있을 수 있으므로, DB 파일을 복사·백업할 때 `.sqlite3` 파일만 따로 옮기지 않는다.
- **`ValueError: ... use pragmas={...} instead`** — `options` 에 `init_command` 를 넣었다. PRAGMA 는 `pragmas` 로 준다.

설계서: [`docs/DESIGN.md`](docs/DESIGN.md) · 결정: [`docs/DECISIONS.md`](docs/DECISIONS.md) · 근거: [`docs/research/`](docs/research/)

## 개발

```bash
uv venv && uv pip install -e . --group dev
uv run --no-sync pytest

scripts/ci-local.sh                 # GitHub Actions CI 를 act 로 로컬 실행 (lint + 전체 매트릭스)
scripts/ci-local.sh test 3.14 6.1   # 매트릭스 한 칸만
scripts/ci-local.sh litestream      # 실제 litestream 바이너리로 통합 테스트 (바이너리가 없어 skip 되면 실패)
```
