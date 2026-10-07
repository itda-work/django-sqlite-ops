# django-sqlite-ops (가칭)

Django 에서 SQLite 를 운영 DB 로 안전하게 쓰게 하는 **운영 도구**다. 권장 설정, 부팅 시 복원 판정, 진단, 복제 헬스, 배포 프로필을 맡는다. DB 백엔드를 바꾸거나 감싸지 않고, 표준 `DATABASES` 설정만 다룬다.

## 현재 상태

| 기능 | 상태 | 이슈 |
|---|---|---|
| 권장 설정 `sqlite_database()` | 구현됨 | [#1](https://github.com/itda-work/django-sqlite-ops/issues/1) |
| boot CLI (복원 판정·복원·잠금) | 구현됨 | [#2](https://github.com/itda-work/django-sqlite-ops/issues/2) · [#3](https://github.com/itda-work/django-sqlite-ops/issues/3) · [#4](https://github.com/itda-work/django-sqlite-ops/issues/4) |
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

컨테이너가 뜰 때 Litestream 복제본과 로컬 DB 를 비교해 **쓸 수 있으면 그대로, 볼륨이 비었으면 복원, 판정할 수 없으면 기동을 거부**한 뒤 다음 명령으로 넘어간다. Django 를 import 하지 않는 독립 CLI 다. `manage.py` 명령은 `django.setup()` 중에 빈 DB 파일을 만들 수 있고, 그러면 복원이 건너뛰어지기 때문이다([DESIGN §4-1](docs/DESIGN.md)).

필요한 것: POSIX(Linux·macOS), PATH 의 `litestream` 0.5.17(검증한 버전만 받는다), DB 마다 복제본이 적힌 Litestream 설정 파일. 설치는 [공식 문서](https://litestream.io/install/)를 따른다.

#### Docker entrypoint

```bash
#!/bin/sh
# entrypoint.sh — boot 가 판정·복원한 뒤 litestream 이 앱을 띄우고 복제한다.
exec python -m django_sqlite_ops.boot \
    --db /data/app.sqlite3 \
    --config /etc/litestream.yml \
    -- litestream replicate -config /etc/litestream.yml \
       -exec "uvicorn proj.asgi:application --host 0.0.0.0 --port 8000"
```

`--` 뒤 명령은 boot 가 판정을 통과했을 때 `exec` 로 boot 프로세스를 대체한다. `migrate` 는 그 명령 안에서(예: `-exec "sh -c 'python manage.py migrate && uvicorn ...'"`) 돌린다.

| 옵션 | 기본값 | 뜻 |
|---|---|---|
| `--db PATH` | (필수) | SQLite DB 경로. Litestream 설정의 `path` 와 같아야 한다 |
| `--config PATH` | (필수) | Litestream 설정 파일 |
| `--on-unknown` | `refuse` | 판정할 수 없을 때: `refuse`(거부) · `restore`(로컬을 격리하고 복원) · `keep-local`(로컬 유지, 헬스에 `unknown_at_boot`) |
| `--init-new` | 꺼짐 | 생애 첫 배포에서 새 DB 로 시작. 첫 배포 뒤에는 끈다 |
| `--adopt-existing` | 꺼짐 | 기존 DB 를 처음 Litestream 에 올린다. 도입 배포 뒤에는 끈다 |
| `--meta-path PATH` | `<db 디렉터리>/.<db 이름>-litestream` | Litestream 설정에 `meta-path` 를 바꿨다면 같은 값을 준다 |
| `--litestream BIN` | `litestream` | 실행 파일 |
| `--ltx-timeout S` | `30` | 원격 TXID 조회 제한 시간(초). 넘으면 거부한다 |
| `--restore-timeout S` | `600` | 복원 제한 시간(초). DB 크기에 맞춰 늘린다 |

#### 무엇을 하나

1. `<db>.boot.lock` 을 잠근다. 이 잠금은 **exec 된 명령에 상속**된다. 그래서 `litestream replicate`(와 그것이 띄운 앱)가 살아 있는 동안 같은 볼륨에서 boot 를 또 돌리면 exit 5 로 막힌다. 다른 머신끼리의 이중 실행은 막지 못한다.
2. 앞선 부팅이 격리 도중 죽었으면(`<db>.stale-<ts>.partial/`) 그 격리부터 마저 끝낸다.
3. 로컬 DB·로컬 메타의 TXID·원격 복제본의 TXID 를 보고 판정한다([DESIGN §4-3](docs/DESIGN.md) 규칙표).
4. 복원이 필요하면 DB 옆 임시 디렉터리(`<db>.restore-…/`)에 먼저 복원하고, 성공했을 때만 로컬을 `<db>.stale-<ts>/` 로 옮긴 뒤 복원본을 제자리에 놓는다. 복원이 실패하면 로컬은 그대로다.
5. DB 가 있으면 읽기 전용으로 `PRAGMA quick_check` 를 한다. DB 가 없으면(새 DB) 만들지 않는다.
6. `<db>.boot-state.json` 에 판정 결과를 남기고 명령을 exec 한다.

stderr 의 `[boot] ...` 줄만 보면 무슨 일이 있었는지 알 수 있다.

```text
[boot] lock: /data/app.sqlite3.boot.lock
[boot] inputs: local_exists=False local_txid=None remote=txid 25
[boot] decision: state=fresh action=restore reason_code=restore_from_remote reason=no local db; restore remote txid 25
[boot] restore: /data/app.sqlite3 -> /data/app.sqlite3.restore-20261008T010203.000001Z-1a2b3c4d/app.sqlite3
[boot] restore: ok, txid 25
[boot] install: /data/app.sqlite3
[boot] integrity: quick_check ok
[boot] state: /data/app.sqlite3.boot-state.json
[boot] exec: litestream replicate -config /etc/litestream.yml -exec uvicorn ...
```

#### 종료 코드

| 코드 | 뜻 | 할 일 |
|---|---|---|
| (exec) | 통과. 이후 코드는 exec 된 명령의 것이다 | — |
| `2` | 거부. 판정할 수 없다 | 아래 사유 코드별 대처 |
| `3` | 무결성 실패(`quick_check` 가 `ok` 아님) | DB 파일을 조사한다. 판정은 `match` 라 `--on-unknown restore` 만으로는 복원되지 않는다. 복제본이 정본이면 DB 파일(과 `-wal`·`-shm`)을 다른 곳으로 옮기고 `--on-unknown restore` 로 다시 부팅한다. 남은 메타가 `stale_meta` 로 격리되고 복원된다 |
| `4` | 복원 실패 | 로그의 `restore failed:` 사유를 본다. 남은 `<db>.restore-…/` 는 조사 뒤 지운다 |
| `5` | 잠금 실패 | 같은 볼륨에서 이미 돌고 있는 컨테이너·프로세스를 멈춘다 |
| `64` | 사용법 오류 | 인자를 고친다. `--` 뒤 명령이 꼭 있어야 한다 |
| `127` | `--` 뒤 명령을 실행할 수 없음 | 명령 이름·PATH·실행 권한을 확인한다 |

#### 생애 첫 배포: `--init-new` 를 한 번 쓰고 끈다

볼륨도 복제본도 비어 있으면 boot 는 거부한다(`no_replica_no_local`). Litestream 은 복제본 경로·prefix 오타와 "복제본 없음"을 같은 빈 목록으로 돌려주므로, 빈 목록만 보고 새 DB 를 시작하면 오타 난 경로에 새 DB 를 올려 기존 복제본을 버리게 된다. 처음 배포할 때만 `--init-new` 를 붙인다. boot 는 DB 파일을 만들지 않고 넘어가고, 앱(`migrate`)이 만든다.

```bash
python -m django_sqlite_ops.boot --db /data/app.sqlite3 --config /etc/litestream.yml \
    --init-new -- litestream replicate -config /etc/litestream.yml -exec "..."
```

첫 복제가 끝나면 **`--init-new` 를 지운다.** 켜 둔 채로 두면, 나중에 볼륨을 잃고 설정의 복제본 경로까지 틀렸을 때 다시 빈 DB 로 시작한다([D-13](docs/DECISIONS.md)).

#### 기존 DB 도입: `--adopt-existing` 을 한 번 쓰고 끈다

이미 운영 중인 DB 를 처음 Litestream 에 올리면 로컬 메타가 없고 복제본은 비어 있다. 이때만 `--adopt-existing` 을 붙인다. 정확히 (로컬 DB 있음, 로컬 메타 없음, 원격 빈 목록) 일 때만 통과시키고 다른 조합에는 효과가 없다. 첫 복제가 끝나면 지운다([D-11](docs/DECISIONS.md)). `--on-unknown keep-local` 을 도입 절차로 쓰지 않는다.

#### 거부됐을 때 (exit 2): 사유 코드별 대처

로그의 `decision: ... reason_code=...` 와 그 아래 `hint:` 줄을 본다.

| 사유 코드 | 뜻 | 대처 |
|---|---|---|
| `no_replica_no_local` | 볼륨도 복제본도 비었다 | 첫 배포면 `--init-new` 를 한 번. 아니면 설정의 복제본 경로·prefix 를 확인한다 |
| `remote_error` | 복제본 조회 실패·타임아웃 | 네트워크·자격 증명·endpoint(평문 HTTP 면 `http://` 를 붙인다)를 고친다. 이 경우는 `--on-unknown` 과 무관하게 거부한다([D-12](docs/DECISIONS.md)) |
| `remote_ahead` | 복제본이 이 볼륨보다 새롭다(옛 볼륨으로 재부팅) | 복제본이 정본이면 `--on-unknown restore` 로 한 번 부팅한다. 로컬은 `<db>.stale-<ts>/` 에 남는다 |
| `no_local_meta` | DB 는 있는데 Litestream 메타가 없다 | 아래 '복원 직후 크래시 복구'. 기존 DB 를 처음 올리는 중이고 복제본이 비었으면 `--adopt-existing` |
| `stale_meta` | DB 없이 메타만 남았다 | `--on-unknown restore` 로 메타를 격리하고 복원한다 |
| `remote_empty` | 복제한 적 있는 DB 인데 복제본이 비었다 | 복제본 경로·prefix·버킷을 확인한다. 로컬을 그대로 쓰려면 `--on-unknown keep-local` |
| `orphan_sidecars` | DB 파일 없이 `-wal`/`-shm`/`-journal` 만 있다 | 파일을 조사한다. 복제본이 정본이면 `--on-unknown restore` 로 격리하고 복원한다 |

`--on-unknown` 은 문제를 푼 그 한 번만 쓰고 원래(`refuse`)로 돌린다. 격리된 `<db>.stale-<ts>/` 는 boot 가 지우지 않는다. 확인한 뒤 직접 지운다.

#### 복원 직후 크래시 복구

복원한 DB 옆에는 Litestream 메타가 없다. `litestream replicate` 가 첫 변경을 기록하기 전에 컨테이너가 죽으면, 다음 부팅은 "DB 는 있는데 메타가 없다"(`no_local_meta`)로 거부된다. 이 DB 는 방금 복제본에서 받은 것이므로 한 번만 `--on-unknown restore` 로 부팅하면 격리하고 다시 복원한다([D-14](docs/DECISIONS.md)).

```bash
python -m django_sqlite_ops.boot --db /data/app.sqlite3 --config /etc/litestream.yml \
    --on-unknown restore -- litestream replicate -config /etc/litestream.yml -exec "..."
```

#### 함정

- DB 경로는 Litestream 설정의 `path` 와 같은 값을 준다. 설정에 없는 DB 면 `remote_error` 로 거부된다.
- 메타 디렉터리는 DB 와 같은 파일시스템에 둔다. 격리는 rename 으로 하므로 다른 볼륨에 있으면 거부한다.
- boot 는 Windows 에서 검증하지 않았다. `fcntl` 이 없으면 exit 64 다.

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
