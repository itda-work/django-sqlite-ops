# django-sqlite-ops (가칭)

Django 에서 SQLite 를 운영 DB 로 안전하게 쓰게 하는 **운영 도구**다. 권장 설정, 부팅 시 복원 판정, 진단, 복제 헬스, 배포 프로필을 맡는다. DB 백엔드를 바꾸거나 감싸지 않고, 표준 `DATABASES` 설정만 다룬다.

## 현재 상태

| 기능 | 상태 | 이슈 |
|---|---|---|
| 권장 설정 `sqlite_database()` | 구현됨 | [#1](https://github.com/itda-work/django-sqlite-ops/issues/1) |
| boot CLI (복원 판정·복원·잠금) | 구현됨 | [#2](https://github.com/itda-work/django-sqlite-ops/issues/2) · [#3](https://github.com/itda-work/django-sqlite-ops/issues/3) · [#4](https://github.com/itda-work/django-sqlite-ops/issues/4) |
| 시스템 체크 | 구현됨 | [#5](https://github.com/itda-work/django-sqlite-ops/issues/5) |
| `sqlite_doctor` 관리 명령 | 구현됨 | [#6](https://github.com/itda-work/django-sqlite-ops/issues/6) |
| 복제 헬스 | 구현됨 | [#7](https://github.com/itda-work/django-sqlite-ops/issues/7) |
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

`INSTALLED_APPS` 에 `"django_sqlite_ops"` 를 넣는다. 그래야 `manage.py check` 가 [시스템 체크](#시스템-체크)를 돈다. 권장 설정은 `settings.py` 에서 함수 하나만 부른다.

## 빠른 시작

```python
# settings.py
from pathlib import Path

from django_sqlite_ops.database import sqlite_database

BASE_DIR = Path(__file__).resolve().parent.parent

INSTALLED_APPS = [
    # ...
    "django_sqlite_ops",
]

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

`sqlite_database()` 를 쓰지 않아도 된다. 직접 쓴 dict 나 dj-lite 가 만든 설정도 [시스템 체크](#시스템-체크)가 같은 권장값 표를 기준으로 검사한다. [`sqlite_doctor`](#sqlite_doctor) 도 같은 표로 실제 값을 비교한다.

### boot CLI

컨테이너가 뜰 때 Litestream 복제본과 로컬 DB 를 비교해 **쓸 수 있으면 그대로, 볼륨이 비었으면 복원, 판정할 수 없으면 기동을 거부**한 뒤 다음 명령으로 넘어간다. Django 를 import 하지 않는 독립 CLI 다. `manage.py` 명령은 `django.setup()` 중에 빈 DB 파일을 만들 수 있고, 그러면 복원이 건너뛰어지기 때문이다([DESIGN §4-1](docs/DESIGN.md)).

필요한 것: POSIX(Linux·macOS), PATH 의 `litestream` 0.5.17(검증한 버전만 받는다), DB 마다 복제본이 적힌 Litestream 설정 파일. 설치는 [공식 문서](https://litestream.io/install/)를 따른다.

**경로 규칙**: `--db`·`--meta-path` 는 **실제 경로**로 준다. 부모 경로에 심볼릭 링크·`..`·없는 디렉터리가 있으면 exit 64 이고, 쓸 실제 경로를 안내한다. Litestream 설정의 `dbs[].path` 에도 **같은 실제 경로**를 쓴다. boot 가 이 경로로 Litestream 을 조회·복원하기 때문이다([D-15](docs/DECISIONS.md)).

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
| `4` | 복원 실패 | 로그의 `restore failed:` 사유를 본다. 임시 복원이 실패했으면 로컬은 그대로이고, 남은 `<db>.restore-…/` 는 조사 뒤 지운다. 격리·설치 중 I/O 실패도 4 다. 격리 도중이면 `<db>.stale-…partial/` 이 남고 다음 부팅이 이어서 끝낸다. 격리를 마친 뒤(설치 중) 실패했으면 다음 부팅이 남은 상태에 따라 다시 복원하거나(로컬이 비었을 때) `no_local_meta` 로 거부한다(아래 '복원 직후 크래시 복구') |
| `5` | 잠금 실패 | 같은 볼륨에서 이미 돌고 있는 컨테이너·프로세스를 멈춘다 |
| `64` | 사용법 오류 | 인자를 고친다. `--` 뒤 명령이 꼭 있어야 한다. 경로가 실제 경로가 아니면 메시지의 실제 경로로 바꾸고 Litestream 설정도 같은 경로로 맞춘다 |
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
- `/data -> /mnt/volume` 처럼 부모가 링크인 경로(`--db /data/app.sqlite3`)는 받지 않는다. 링크를 따라간 실제 경로(`/mnt/volume/app.sqlite3`)를 boot 인자와 Litestream 설정 양쪽에 쓴다. 경로는 파일 이름으로 끝나야 한다.
- `--meta-path` 를 DB 디렉터리 자체나 그 조상으로 두지 않는다. 메타가 DB 를 포함하면 격리가 필요할 때 거부된다(exit 2).
- 메타 디렉터리는 DB 와 같은 파일시스템에 둔다. 격리는 rename 으로 하므로 다른 볼륨에 있으면 거부한다.
- **격리가 필요할 때(`restore` 조치) DB·`-wal`·`-shm`·`-journal`·메타 디렉터리 경로가 심볼릭 링크면 거부한다(exit 2).** 링크를 옮기면 실체가 밖에 남기 때문이다(v0.1 제약). 판정만 하고 진행하는 부팅(`match` 등)에서는 메타 디렉터리 링크를 따라가므로 평소에는 드러나지 않는다. 볼륨 안에 실제 파일·디렉터리로 둔다.
- DB 나 `--meta-path` 의 이름을 `manifest.json` 으로 하지 않는다. 격리 디렉터리의 예약 이름이라 격리가 필요할 때 거부된다.
- 격리 도중 죽어 `<db>.stale-…partial/` 이 남았다면 **같은 `--db`·`--meta-path`** 로 다시 부팅한다. 다르면 판정할 수 없어 거부한다. 그 안의 파일을 손으로 바꾸지 않는다.
- `<db>.boot.lock` 을 지우지 않는다. flock 잠금은 파일에 걸리므로, 앱이 도는 중에 잠금 파일을 지우거나 바꾸면 다음 boot 가 새 파일을 잠가 이중 실행을 막지 못한다. 잠금 파일이 링크거나 정규 파일이 아니면 exit 5 다.
- boot 는 Windows 에서 검증하지 않았다. `fcntl` 이 없으면 exit 64 다.

### 시스템 체크

`INSTALLED_APPS` 에 `"django_sqlite_ops"` 를 넣으면 `manage.py check` 가 `DATABASES` 의 SQLite 별칭(`ENGINE` 이 `django.db.backends.sqlite3`)을 [권장값](#기본값)과 비교한다. **설정만 읽고 DB 를 열지 않는다.** 그래서 DB 파일이 없어도, 빌드 단계에서도 돌릴 수 있다. 실제로 적용된 PRAGMA 값은 [`sqlite_doctor`](#sqlite_doctor)가 본다.

```bash
python manage.py check --deploy   # W001·W002·W004 는 --deploy 일 때만 나온다
```

| ID | 조건 | 언제 | 고치는 법 |
|---|---|---|---|
| `sqlite_ops.E001` | `SQLITE_OPS` 가 dict 가 아니거나 `PROFILE` 이 알 수 없는 이름 | 항상 | `PROFILE` 을 `"single-server"`·`"single-server-multiproc"` 중 하나로. 이 오류가 있으면 다른 체크는 돌지 않는다 |
| `sqlite_ops.E002` | `SQLITE_OPS["HEALTH"]` 가 잘못됨: dict 가 아님, 모르는 키, `DATABASES` 가 비었거나 `DATABASES` 에 없는·sqlite3 가 아닌 별칭, `litestream_config` 없음, `REFRESH` 가 양수가 아님, `BACKLOG_GRACE` 가 음수 등. `HEALTH` 가 없으면 검사하지 않는다 | 항상 | [복제 헬스](#복제-헬스)의 설정 형식대로 고친다 |
| `sqlite_ops.W001` | `OPTIONS["transaction_mode"]` 가 `IMMEDIATE` 가 아님(없음 포함, 대소문자 무시) | `--deploy` | `sqlite_database()` 를 쓰거나 `"transaction_mode": "IMMEDIATE"` 를 넣는다 |
| `sqlite_ops.W002` | `OPTIONS["init_command"]` 에서 `journal_mode` 가 `WAL` 로 설정되지 않음, 또는 `journal_mode` 를 언급하는 문장의 형식을 판정할 수 없음 | `--deploy` | `sqlite_database()` 를 쓰거나 `init_command` 에 `PRAGMA journal_mode=WAL` 을 넣는다 |
| `sqlite_ops.W003` | 이름이 Litestream VFS(`vfs=litestream` 이 든 `file:` URI)인 별칭의 `CONN_MAX_AGE` 가 `None` 이 아님, `ASGI_APPLICATION` 미설정(WSGI) | 항상 | 그 별칭에 `"CONN_MAX_AGE": None`. WSGI 실측에서 요청당 1,008ms → 1.7ms |
| `sqlite_ops.W004` | `NAME` 의 URI 로 별칭 역할(쓰기·읽기 전용·메모리·VFS)을 판정할 수 없음. 이때 W001·W002 는 내지 않는다 | `--deploy` | authority 는 비우거나 `localhost` 로(`file:///path`), `%00` 을 빼고, URI 쿼리 키 `mode`·`immutable`·`vfs` 를 한 번씩, 표준 표기(`mode=ro\|rw\|rwc\|memory`, `immutable=1\|0`)로 쓴다 |

- 기준값은 `sqlite_database()` 와 같은 권장값 표에서 읽는다. 프로필은 `SQLITE_OPS["PROFILE"]` 이고 없으면 `"single-server"` 다.
- `sqlite_database()` 로 만든 설정은 경고가 없다(W003 은 VFS 별칭에 `CONN_MAX_AGE` 를 따로 줘야 한다).
- W002 는 Django 처럼 `init_command` 를 `;` 로 나눈 각 문장을 본다. SQL 주석(`-- …`, `/* … */`)은 지우고 본다. `PRAGMA journal_mode = wal`, `PRAGMA main.journal_mode=WAL`, `PRAGMA "journal_mode"=WAL`, `PRAGMA journal_mode('wal')` 처럼 대소문자·공백·인용 식별자·`main.` 접두가 달라도 인정하고, 여러 번 설정했으면 마지막 값을 본다. `temp.` 같은 다른 스키마는 이 DB 의 모드를 바꾸지 않으므로 세지 않는다.
- 정적 체크는 SQL 파서가 아니다. `journal_mode` 를 언급하는데 위 형식이 아닌 문장(`SELECT … pragma_journal_mode`, 알 수 없는 값 등)이 있으면 **판정할 수 없다는 W002** 를 낸다. 메시지에 그 문장이 나온다. `PRAGMA journal_mode=WAL` 형식으로 고쳐 쓴다.
- 별칭의 역할은 `NAME` 으로 판별한다. `NAME` 이 `Path` 면 문자열로 바꿔 본다. `file:` URI 는 SQLite 처럼 첫 `?`·`#` 로 파일명과 쿼리를 나눈 **뒤** 퍼센트 디코딩한다(`file:%3Amemory%3A` 는 메모리, `…/q%3Fmode%3Dro.sqlite3` 는 파일명이 `q?mode=ro.sqlite3` 인 쓰기 DB).
  - 메모리 DB — `:memory:`, 파일명이 정확히 `:memory:` 인 URI(`file::memory:`, `file::memory:?cache=shared`), `mode=memory`: WAL 이 의미 없어 W002 를 건너뛴다. `file::memory:backup.sqlite3` 는 실제 파일이라 검사한다.
  - 읽기 전용 — `mode=ro`, `immutable` 참값(`1`·`yes`·`true`·`on`, 대소문자 무시): W001·W002 를 건너뛴다. 읽기 전용 연결에 `PRAGMA journal_mode=WAL` 을 넣으면 `attempt to write a readonly database` 로 연결이 깨지거나(`mode=ro`) 아무 효과가 없다(`immutable`). `immutable` 거짓값(`0`·`no`·`false`·`off`)은 쓰기 DB 다.
  - Litestream VFS(`vfs=litestream`): W001·W002 를 건너뛴다. W003 은 그대로 본다.
  - **판정할 수 없음 → W004** — `mode`·`immutable`·`vfs` 가 두 번 이상 나오거나(`mode=rwc&mode=ro`), `mode` 가 `ro`·`rw`·`rwc`·`memory` 가 아니거나(`mode=RO`, `mode=ro%00x`), `immutable` 이 위 불리언 표기가 아닐 때(`immutable=2`), 디코딩한 파일명이나 쿼리 키·값에 NUL(`%00`)이 있을 때(`mode%00x=ro` — SQLite 는 NUL 앞까지만 읽어 역할 키가 숨는다. URI 가 아닌 일반 경로의 `%00` 은 글자 그대로라 해당 없다), `file://` 뒤 authority 가 빈 값이나 정확히 `localhost` 가 아닐 때(`file://example.com/…`, `file://LOCALHOST/…` — SQLite 는 연결 오류를 낸다. `file://localhost/srv/app.sqlite3`·`file:///srv/app.sqlite3` 는 로컬 파일이다). 이때는 W001·W002 를 내지 않는다. SQLite 는 중복 키를 순서에 따라 다르게 해석해서(`mode=rwc&mode=ro` 는 읽기 전용, `mode=ro&mode=rwc` 는 연결 오류) 쓰기 권고를 따르면 연결이 깨질 수 있기 때문이다. W004 는 쓰기 설정을 넣어도 사라지지 않는다. 쿼리 키를 한 번씩, 표준 표기로 고쳐 쓴다.
- W003 은 ASGI 에서는 내지 않는다. ASGI 의 영속 연결은 아직 재지 않았고, Django 는 async 에서 영속 연결을 끄라고 권한다.
- 경고를 끄려면 Django 표준 `SILENCED_SYSTEM_CHECKS` 를 쓴다. 이 앱의 체크만 돌리려면 `check --tag sqlite_ops`.

```python
# settings.py — 직접 쓴 설정도 같은 기준으로 검사된다
INSTALLED_APPS = ["django_sqlite_ops"]

SQLITE_OPS = {"PROFILE": "single-server-multiproc"}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "/srv/app/app.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL;PRAGMA busy_timeout=5000",
        },
    },
}
```

### `sqlite_doctor`

`manage.py check` 는 설정 문자열만 본다. `sqlite_doctor` 는 **DB 에 실제로 연결해** `init_command` 가 실행된 뒤의 값을 보고, 파일시스템 종류·Litestream 설정·채널 레이어까지 한 번에 진단한다. 배포 직후, 설정을 바꾼 뒤, 장애를 조사할 때 명시적으로 실행한다.

```bash
python manage.py sqlite_doctor
python manage.py sqlite_doctor --database default --litestream-config /etc/litestream.yml
python manage.py sqlite_doctor --json          # 모니터링·스크립트용
```

| 옵션 | 뜻 |
|---|---|
| `--database ALIAS` | 볼 별칭(여러 번 줄 수 있다). 없으면 sqlite3 엔진 별칭 전부 |
| `--litestream-config PATH` | Litestream 설정 파일. 주면 그 DB 목록을 `DATABASES` 와 대조한다. 없으면 이 섹션은 건너뛴다. 상대 경로는 명령을 실행한 디렉터리 기준이다 |
| `--litestream BIN` | Litestream 바이너리(기본 PATH 의 `litestream`, 0.5.17 만 받는다) |
| `--json` | JSON 출력(스키마 `version: 1`, [DESIGN §6-2](docs/DESIGN.md)) |

> **주의 — 연결하면 `init_command` 가 실행되고 `-wal`·`-shm` 이 생길 수 있다.** Django 는 연결할 때마다 `OPTIONS["init_command"]` 를 실행하므로 진단이 DB 를 바꿀 수 있다. 예를 들어 `PRAGMA journal_mode=WAL` 은 파일에 남는다. 또 WAL 모드 DB 는 연결만 해도 SQLite 가 `-wal`·`-shm` 파일을 만든다(`init_command` 가 없어도). 쓰기 별칭은 앱이 연결할 때와 같은 일이므로 그대로 연결한다. 운영 DB 에서 돌린다는 점을 알고 실행한다.
>
> **읽기 전용 별칭은 파일을 바꾸지 않는다.** DB 헤더가 WAL 이면 `-shm` 이 있든 없든 연결하지 않고 `unknown` 으로 적는다. WAL 읽기는 SQLite 가 본래 `-shm` 에 쓰고 `-wal` 을 만들 수 있기 때문이다(재현함: 없던 `-wal` 생성, 빈 `-shm` 이 32KB 로 커짐, 쓰는 프로세스가 열려 있거나 비정상 종료 뒤 남은 `-shm` 의 내용 변경). `immutable=1` URI 는 사이드카를 건드리지 않으므로(실측) 연결한다. **읽기 전용 WAL 복제본은 쓰기 쪽 별칭이나 `immutable` URI 로 진단한다.** 예: `DATABASES["ro"]` 가 `file:/srv/app.sqlite3?mode=ro` 면 같은 파일의 쓰기 별칭(`default`)을 `--database default` 로 보거나, 진단용으로 `file:/srv/app.sqlite3?immutable=1` 별칭을 둔다(`immutable` 은 `-wal` 의 커밋을 무시하므로 쓰는 프로세스가 도는 중에는 최신 내용이 아닐 수 있다).
>
> **DB 파일이 없으면 연결하지 않는다.** SQLite 는 없는 파일에 연결하면 빈 DB 를 만든다. 그러면 boot 가 다음 부팅에서 복원 대신 그 빈 DB 를 판정하게 된다. 그래서 파일이 없으면 오류로 적고 연결하지 않는다.

무엇을 보나:

- **database** — 별칭마다 Django 가 실제로 여는 경로(`OPTIONS["database"]` 가 있으면 `NAME` 대신 그것. 연결하지 않고 Django 의 연결 매개변수에서 읽는다)를 기준으로 역할(쓰기·읽기 전용·메모리·VFS), 파일 크기, `-wal` 크기(연결 전), 실제 `transaction_mode`·`journal_mode`·`synchronous`·`busy_timeout`·`foreign_keys`·SQLite 버전. [권장값](#기본값)과 다르면 `warn`. 읽기 전용 별칭은 쓰기 권고(`transaction_mode`·`journal_mode`·`synchronous`)를 비교하지 않는다. 메모리 DB 는 "메모리 DB" 로만 적는다. Litestream VFS 별칭과 역할을 판정할 수 없는 별칭(W004, 로컬이 아닌 URI authority 포함)은 연결하지 않고 `unknown`. 연결 실패는 `error`.
- **mount** — 위의 실제 경로로 DB 파일(없으면 존재하는 상위 디렉터리)의 파일시스템 종류. NFS·SMB/CIFS·AFP·sshfs 같은 네트워크 파일시스템이면 `warn` 이다. SQLite 의 파일 잠금을 믿을 수 없고 WAL 이 동작하지 않는다([SQLite 문서](https://www.sqlite.org/useovernet.html)). 로컬로 확인되지 않은 종류(알 수 없는 FUSE 등)는 `unknown`.
- **litestream** — `litestream databases -config PATH -json` 으로 설정의 DB 목록을 읽어 실제 경로로 대조한다. 쓰기 별칭이 설정에 없으면 `warn`(복제되지 않음). 단 channels-lite 전용 채널 DB(앱 DB 와 다른 파일)는 복제에서 빼는 것이 규칙이라 이 경고를 내지 않는다. 설정 경로가 실제 경로가 아니면(부모에 링크) `warn` — boot 가 거부하는 구성이다(D-15). 상대 경로·`dir:` 항목은 Litestream 의 작업 디렉터리 기준으로 풀려 대조할 수 없으므로 `warn`. 설정에만 있는 DB 는 `ok`(정보). 설정 파일 없음·깨진 YAML·바이너리 없음은 `error`.
- **channels** — `CHANNEL_LAYERS` 를 읽기만 한다(`channels` 를 import 하지 않는다). 백엔드별 의미론을 한 줄로 요약한다. `InMemoryChannelLayer` 는 프로필이 `single-server-multiproc` 이면 `warn`. channels-lite 는 채널 DB 가 앱 DB 와 같은 파일이면 `warn`, Litestream 설정의 복제 대상이면 `warn`. 모르는 백엔드(`channels_redis` pub/sub 포함)는 `unknown`.

출력 예:

```text
[settings]
  OK      -  profile  single-server

[database]
  OK      default  role              write
  OK      default  file              8192  — bytes
  OK      default  wal               -  — no -wal file
  OK      default  transaction_mode  IMMEDIATE (expected IMMEDIATE)
  OK      default  journal_mode      WAL (expected WAL)
  WARN    default  synchronous       FULL (expected NORMAL)  — differs from the recommended value (DESIGN §6-0)
  OK      default  busy_timeout      5000 (expected 5000)
  OK      default  foreign_keys      1
  OK      default  sqlite_version    3.53.1

[mount]
  OK      default  filesystem  ext4  — local filesystem

[litestream]
  OK      -        config      /etc/litestream.yml  — 1 database(s)
  OK      default  replicated  /srv/app/app.sqlite3  — in the config

[channels]
  OK      default  backend  channels.layers.InMemoryChannelLayer  — in-memory: one process only; messages do not reach other processes or workers

summary: 1 warning(s), 0 error(s), 0 unknown -> exit 1
```

| 종료 코드 | 뜻 |
|---|---|
| 0 | 문제 없음 |
| 1 | 경고 또는 판정할 수 없는 항목(`unknown`)이 있음 |
| 2 | 오류가 있음(DB 파일 없음, 연결 실패, Litestream 설정을 읽지 못함 등) |

`call_command("sqlite_doctor")` 로 부르면 종료 코드가 0 이 아닐 때 `SystemExit` 가 난다.

### 복제 헬스

"지금 복제가 따라오는가"를 `caught_up / backlog / unknown` 으로 보고하는 JSON 엔드포인트다. Litestream 은 S3 가 끊겨도 로그·`status`·메트릭에 아무것도 남기지 않으므로(실측 D3), 헬스는 Litestream 의 자기 보고 대신 두 가지를 직접 본다.

1. **TXID 비교 (로컬 L0 → 복제본)** — 로컬 메타의 최대 TXID(`.<db>-litestream/ltx/0/`)와 복제본의 최대 TXID(`litestream ltx -level all -json`). 업로드가 막히면(S3 끊김) 로컬이 앞선다.
2. **WAL 위치 비교 (DB → 로컬 L0)** — 최신 로컬 L0 파일의 헤더에는 그 L0 가 원래 WAL 의 어디까지를 담았는지(WAL 오프셋·크기·salt)가 있다. 현재 `-wal` 파일의 헤더와 프레임 헤더를 읽어(salt·누적 체크섬이 맞는 프레임만) 그 위치 **뒤에 커밋이 있는지** 본다. Litestream 자신도 다음 동기화를 이 위치에서 이어 간다. `litestream replicate` 가 죽거나 멈추면 TXID 는 로컬·원격이 같은 채로 멈춰 1 만으로는 그 뒤의 쓰기가 보이지 않는데, 2 가 그것을 잡는다.

**DB 연결을 열지 않는다.** 모두 파일 읽기(`-wal` 은 읽기 전용으로 열어 헤더만)와 `litestream` 명령으로만 본다.

> **파일 시각은 "그 커밋이 복제본에 들어갔다"의 증거가 아니다.** Litestream 은 L0 를 만들 때 담을 WAL 범위를 먼저 정하고 DB 를 복사한 뒤 파일을 닫는다. 그래서 복사 중에 들어온 커밋은 L0 에 없는데도 L0 의 mtime 은 그 커밋보다 늦다(재현함: 그 상태에서 Litestream 이 멈추면 복원본에 행이 하나 없다). 헬스는 시각이 아니라 WAL 위치로 판정한다. 파일 시각은 WAL 로 판정할 수 없을 때 `backlog` 쪽으로만 쓰는 보조 근거다(아래).

```python
# settings.py
from django_sqlite_ops.database import sqlite_database

INSTALLED_APPS = ["django_sqlite_ops"]

DATABASES = {"default": sqlite_database("/srv/app/app.sqlite3")}

SQLITE_OPS = {
    "HEALTH": {
        "DATABASES": {
            # 별칭마다 Litestream 설정 파일. meta_path(설정에 meta-path 를 바꿨을 때)와
            # litestream(바이너리, 기본 PATH 의 litestream)은 선택이다.
            "default": {"litestream_config": "/etc/litestream.yml"},
        },
        "REFRESH": 15,  # 초, 원격 조회 주기 (기본 15)
        "BACKLOG_GRACE": 60,  # 초, 로컬이 앞선 상태를 backlog 로 볼 때까지 (기본 60)
    },
}
```

```python
# urls.py
from django.urls import path

from django_sqlite_ops.health import health_view

urlpatterns = [
    path("internal/sqlite-health", health_view),
]
```

응답 예(`GET /internal/sqlite-health`):

```json
{
  "version": 1,
  "status": "caught_up",
  "refresh": 15.0,
  "backlog_grace": 60.0,
  "databases": {
    "default": {
      "status": "caught_up",
      "code": "in_sync",
      "reason": "local and replica are at the same TXID and every -wal commit is in the latest L0",
      "path": "/srv/app/app.sqlite3",
      "local_txid": "00000000000004d2",
      "remote_txid": "00000000000004d2",
      "checked_at": "2026-10-08T01:02:18.412Z",
      "age": 3.2,
      "backlog_since": null,
      "pending_since": null,
      "db_changed_at": "2026-10-08T01:02:11.020Z",
      "ltx_at": "2026-10-08T01:02:11.533Z",
      "wal": {"evidence": "in_sync", "reason": "every -wal commit is within the latest L0",
              "ltx_wal_end": 4152, "wal_commit_end": null},
      "boot_state": {"state": "match", "action": "proceed", "reason_code": "local_current",
                     "reason": "...", "unknown_at_boot": false,
                     "litestream_version": "0.5.17", "at": "2026-10-08T01:02:03Z"},
      "boot_state_error": null
    }
  }
}
```

| 상태 | `code` | 뜻 |
|---|---|---|
| `caught_up` | `in_sync` | 로컬 TXID == 복제본 TXID, 그리고 `-wal` 의 커밋이 모두 최신 L0 범위 안 |
| `caught_up` | `local_ahead_within_grace` | 로컬 TXID 가 앞서 있지만 아직 올라가지 않은 TXID 가 기다린 시간이 `BACKLOG_GRACE` 보다 짧음(정상 업로드 지연). `backlog_since` 가 함께 나온다 |
| `caught_up` | `db_changed_within_grace` | `-wal` 에 최신 L0 뒤의 커밋이 있지만 `BACKLOG_GRACE` 보다 짧음(Litestream 이 곧 L0 를 쓴다). `pending_since` 가 함께 나온다 |
| `backlog` | `local_ahead` | 아직 올라가지 않은 로컬 TXID 가 `BACKLOG_GRACE` 이상 기다림. 업로드가 막혔거나 쓰기를 못 따라간다(S3 끊김, 자격 증명 만료) |
| `backlog` | `db_not_replicated` | `-wal` 에 최신 L0 뒤의 커밋이 `BACKLOG_GRACE` 이상 남아 있음(또는 WAL 근거가 없을 때 파일 시각이 그만큼 L0 보다 새로움). `litestream replicate` 가 죽었거나 멈췄을 수 있다 |
| `unknown` | `no_wal_evidence` | `-wal` 로 판정할 수 없음: `-wal` 이 없거나(마지막 연결이 닫혀 지워짐) 비었음, L0 이후 WAL 이 다시 시작됐는데 아직 커밋이 없음 등. **이때는 `caught_up` 으로 단정하지 않는다** |
| `unknown` | `file_time_backwards` | WAL 근거가 없는데 DB·`-wal`·L0 의 파일 시각이 앞선 관측보다 뒤로 감(시계 변경 등) |
| `unknown` | `remote_error` · `remote_empty` · `no_local_meta` · `remote_ahead` · `unknown_at_boot` · `path_not_real` · `not_file_db` · `refresh_failed` · `not_checked` · `stale` | 판정할 수 없음: 원격 조회 실패, 복제본이 빈 목록(경로·prefix 오타와 구분되지 않는다), 로컬 메타 없음(`litestream replicate` 가 돌지 않음), **복제본이 로컬보다 앞섬**(다른 기계가 같은 복제본에 쓰는 중일 수 있다), 부팅 상태 파일의 `unknown_at_boot`(boot 가 `--on-unknown keep-local` 로 진행함), 경로 규칙 위반(D-15, 아래), 파일 DB 가 아님, 갱신 중 예외, 아직 첫 조회 전, 마지막 결과가 `REFRESH × 3` 보다 오래됨(갱신 스레드가 멈춤) |

`code` 는 고정된 값이라 알람 규칙에 쓸 수 있다. 설정이 없거나 틀리면 최상위에 `code`(`not_configured`·`invalid_config`)와 `reason` 이 붙는다.

**WAL 위치 근거** (형식: [SQLite WAL](https://www.sqlite.org/fileformat2.html#walformat), superfly/ltx v0.5.2 `Header`, Litestream 0.5.17 `db.go`):
- 판정: 현재 `-wal` 의 salt 가 최신 L0 의 salt 와 같으면, L0 끝(`WALOffset + WALSize`) 뒤에 유효한 커밋 프레임이 있으면 pending, 없으면 in_sync. salt 가 다르면(L0 이후 WAL 이 다시 시작됨) 새 salt 의 커밋이 있으면 pending, 없으면 판정할 수 없음.
- 프레임은 salt 와 누적 체크섬이 맞을 때만 센다(SQLite 의 복구 규칙과 같다). 쓰다 만 프레임은 커밋으로 세지 않는다. L0 끝 뒤의 프레임만, 첫 커밋까지만 읽는다(한 번에 최대 64 MiB).
- 잠금 없이 읽으므로 읽는 도중 체크포인트가 `-wal` 을 비우거나 다시 시작할 수 있다. 그래서 "L0 뒤 커밋 없음"이라고 답하기 전에 처음 연 `-wal` 이 그대로인지(같은 파일, 같은 크기, 같은 헤더) 다시 보고, 바뀌었으면 한 번만 다시 읽는다. 그래도 바뀌면 판정하지 않는다(`no_wal_evidence`).
- pending 지속 시간은 처음 관측한 때부터 monotonic 으로 세고, 최신 L0 의 WAL 위치가 바뀌면(Litestream 이 진행 중) 다시 센다. 쓰기가 계속되는 바쁜 DB 도 Litestream 이 살아 있으면 `backlog` 가 되지 않는다(실측: 30ms 간격 쓰기 30초, 0.5초마다 판정, grace 3초에서 모두 `caught_up`).
- Litestream 이 도는 동안에는 Litestream 이 연결을 쥐고 있어 `-wal` 이 남으므로 이 근거가 늘 있다.

**보조 근거: 파일 시각** — WAL 로 판정할 수 없을 때만 쓴다(주로 Litestream 이 멈춘 뒤 앱의 마지막 연결이 닫혀 `-wal` 이 지워졌을 때).
- DB 변경 시각 `max(mtime(DB), mtime(DB-wal))` 이 최신 L0 의 mtime 보다 늦은 상태가 `BACKLOG_GRACE` 이상 이어지면 `backlog`(`db_not_replicated`, 사유에 "file times only"). 아니면 **`unknown`(`no_wal_evidence`) — 시각만으로 `caught_up` 이라고 하지 않는다.**
- Litestream 이 멈춰 있으면 앱의 마지막 연결이 닫힐 때의 체크포인트(읽기만 했어도)와 `replicate -once` 의 종료가 DB mtime 을 바꾼다(실측). 그래서 이 경로의 `db_not_replicated` 는 "DB 가 쓰이는데 Litestream 이 진행하지 않는다"는 뜻이다.
- 파일 시각이 앞선 관측보다 뒤로 가면(벽시계 역행, 파일 복사) `unknown`(`file_time_backwards`)이고 앞선 근거를 지우지 않는다. 다른 프로세스가 DB 파일을 `touch`·복사·덮어쓰면 이 경로는 틀릴 수 있다.
- 드문 경우: Litestream 이 도는데 `-wal` 이 TRUNCATE 체크포인트로 0바이트가 되고 그 뒤 쓰기가 없으면(기본 설정에서는 WAL 이 약 500MB 를 넘을 때만) 다음 쓰기까지 `unknown` 이거나, 파일 시각 때문에 grace 뒤 `backlog` 로 보일 수 있다.

- **전체 `status` 는 별칭 중 가장 나쁜 것**이다(`unknown` > `backlog` > `caught_up`). `unknown` 이 가장 나쁜 이유: 복제가 따라오는지조차 말할 수 없다는 뜻이라, 뒤처진 것을 아는 `backlog` 보다 더 큰 문제를 감출 수 있다.
- **요청마다 S3 를 부르지 않는다.** 프로세스마다 데몬 스레드 하나가 `REFRESH` 초마다 조회하고 뷰는 마지막 결과를 읽기만 한다. 스레드는 **첫 헬스 요청 때** 시작하므로 배포 직후 첫 응답은 `unknown`("not checked yet")이다. 관리 명령·마이그레이션에서는 스레드가 돌지 않는다. gunicorn `--preload` 처럼 포크하는 서버에서도 워커마다 PID 를 보고 다시 시작한다.
- **지속 시간(`backlog_since`·`pending_since` 부터의 시간, `REFRESH × 3` 판정)은 프로세스 메모리에서 monotonic 시계로 잰다.** 주 근거(TXID·WAL 위치)는 벽시계를 쓰지 않으므로 벽시계가 NTP 로 뒤로 가도 판정이 되돌아가지 않는다. 보조 근거(파일 시각)는 벽시계를 쓰므로 역행을 보면 `unknown` 으로 둔다. 응답의 시각 문자열(UTC, 밀리초)은 표시용 벽시계다. 앱을 재시작하면 추적이 초기화되어, 재시작 직후에는 오래된 backlog 도 `BACKLOG_GRACE` 동안 `caught_up` 으로 보인다. 워커마다 따로 추적하므로 워커별 응답이 몇 초 다를 수 있다.
- **`ATOMIC_REQUESTS` 를 켠 별칭이 있어도 헬스 요청은 트랜잭션으로 감싸지 않는다.** Django 는 감싸려고 뷰 실행 전에 연결을 연다(그러면 DB 파일이 생길 수 있다). `health_view` 는 모든 별칭에서 빠지도록 표시돼 있다. 헬스 뷰를 다른 데코레이터로 감쌀 때는 `functools.wraps` 로 이 표시(`_non_atomic_requests`)를 옮긴다.
- **"마지막 업로드 시각"은 쓰지 않는다.** 쓰기가 없는 정상 DB 도 업로드 시각은 오래되기 때문이다.
- 부팅 상태 파일 `<db>.boot-state.json`([boot CLI](#boot-cli)가 씀)을 `boot_state` 로 보여 준다. 파일이 없거나 형식이 틀리면 `boot_state_error` 에 그 사실만 적고 상태는 바꾸지 않는다(boot 를 쓰지 않는 배포도 있다). `unknown_at_boot` 가 참이면 다음 정상 부팅까지 `unknown` 이다.
- DB 경로는 Django 가 실제로 여는 경로(`OPTIONS["database"]` 포함, [`sqlite_doctor`](#sqlite_doctor)와 같은 방식)다. **boot 와 같은 실제 경로 규칙(D-15)**을 따른다: 부모 경로에 심볼릭 링크·`..` 가 있거나 DB 파일 자체가 링크면 그 별칭은 `unknown` 이다. `meta_path` 도 같다(`..` 를 접지 않고 쓴 그대로 검사한다. 상대 경로는 작업 디렉터리만 앞에 붙인다). Litestream 설정의 `dbs[].path` 도 같은 실제 경로여야 한다.
- 설정이 잘못되면 `manage.py check` 가 `sqlite_ops.E002` 를 내고, 뷰는 `unknown` 과 사유를 돌려준다.

**HTTP 상태는 항상 200 이다.** 헬스 엔드포인트를 로드밸런서의 헬스 체크로 쓰면, 복제가 뒤처지거나 S3 가 끊겼다는 이유로 앱이 서비스에서 빠진다. 복제 지연은 데이터 손실 위험이지 요청을 못 받는 상태가 아니므로 앱을 내리면 안 된다. 모니터링은 **본문의 `status`** 로 알람을 건다(`backlog` 나 `unknown` 이 몇 분 이어지면 경보). HTTP 코드로만 판정할 수 있는 모니터를 쓴다면 `?strict=1` 을 붙인다. 그러면 `caught_up` 이 아닐 때 503 이다. **로드밸런서 헬스 체크에는 `strict` 를 쓰지 않는다.**

**인증하지 않는다. 내부망에만 노출한다.** 응답에는 DB 파일 경로, TXID, 파일 시각, 부팅 상태가 들어간다. **사유(`reason`)는 고정 문장에 경로·TXID·초만 넣는다.** Litestream 의 stderr 나 예외 원문은 응답에 넣지 않는다(Litestream 은 설정 오류 메시지에 endpoint 의 `user:password@` 까지 그대로 찍는다 — 재현함). 원문은 로거 `django_sqlite_ops.health` 에 WARNING 으로, 같은 메시지는 한 번만 남는다. 로그에 남기기 전에 URL 의 사용자 정보, 쿼리의 서명·토큰류 값, `secret-access-key: …` 같은 `키: 값` 을 가린다(`health.redact()`). 완전한 비밀 탐지기는 아니므로 로그도 접근을 제한한다. 공개 URL 에 붙여야 한다면 웹 서버에서 IP 로 막거나 자체 인증 뷰로 감싼다.

```python
# settings.py — 원문 진단 로그를 보려면
LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"django_sqlite_ops.health": {"handlers": ["console"], "level": "WARNING"}},
}
```

헬스는 "지금 복제가 따라오는가"만 말한다. 복제본으로 실제 복구할 수 있는지는 주기적인 복원 검증으로 따로 본다(예정).

### 배포 프로필

예정. `single-server`, `single-server-multiproc` 의 채널 레이어·프로세스 구성 문서. [#9](https://github.com/itda-work/django-sqlite-ops/issues/9)

### channels-lite 패치

예정. channels-lite aio 중복 배달을 버전 게이트 패치로 막는다. [#8](https://github.com/itda-work/django-sqlite-ops/issues/8)

## 문제 해결

- **`options={"timeout": ...}` 을 줬는데 대기 시간이 그대로다.** `busy_timeout`(밀리초)이 `timeout`(초)보다 우선한다. 위 [`timeout` 과 `busy_timeout`](#timeout-과-busy_timeout) 을 본다.
- **DB 파일 옆에 `-wal`, `-shm` 파일이 생긴다.** WAL 모드의 정상 동작이다. 연결이 열려 있는 동안 커밋된 내용 일부가 `-wal` 에만 있을 수 있으므로, DB 파일을 복사·백업할 때 `.sqlite3` 파일만 따로 옮기지 않는다.
- **`ValueError: ... use pragmas={...} instead`** — `options` 에 `init_command` 를 넣었다. PRAGMA 는 `pragmas` 로 준다.
- **`manage.py check` 에 `sqlite_ops.*` 가 안 나온다.** `INSTALLED_APPS` 에 `"django_sqlite_ops"` 가 있는지, W001·W002 라면 `--deploy` 를 붙였는지 본다.

설계서: [`docs/DESIGN.md`](docs/DESIGN.md) · 결정: [`docs/DECISIONS.md`](docs/DECISIONS.md) · 근거: [`docs/research/`](docs/research/)

## 개발

```bash
uv venv && uv pip install -e . --group dev
uv run --no-sync pytest

scripts/ci-local.sh                 # GitHub Actions CI 를 act 로 로컬 실행 (lint + 전체 매트릭스)
scripts/ci-local.sh test 3.14 6.1   # 매트릭스 한 칸만
scripts/ci-local.sh litestream      # 실제 litestream 바이너리로 통합 테스트 (바이너리가 없어 skip 되면 실패)
```
