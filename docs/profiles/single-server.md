# 배포 프로필: `single-server`

서버(머신) 한 대, 앱 프로세스 하나, SQLite(WAL) 하나, Litestream 복제 하나. 이 패키지가 가정하는 가장 작은 운영 구성이다.

| 항목 | 값 |
|---|---|
| 머신 | 1대 (쓰는 머신은 언제나 1대) |
| 앱 프로세스 | ASGI 서버 워커 1개 (`uvicorn --workers 1`) |
| 채널 레이어 | `channels.layers.InMemoryChannelLayer` |
| 복제 | Litestream 0.5.17, `litestream replicate -exec` 가 앱을 띄운다 |
| 부팅 | `python -m django_sqlite_ops.boot` 가 판정·복원한 뒤 exec |
| 설정 | `sqlite_database(..., profile="single-server")`, `SQLITE_OPS["PROFILE"] = "single-server"` |

워커를 둘 이상 띄우거나 채널 메시지가 프로세스 사이를 오가야 하면 [`single-server-multiproc`](single-server-multiproc.md) 을 쓴다.

수치는 모두 `docs/research/` 의 실측이고, 문장마다 출처를 단다. Docker 랩 실측(D1~D6)은 [`litestream-django-docker.md`](../research/litestream-django-docker.md), 호스트 실측(S1~S8, RPO)은 [`litestream-django-scenarios.md`](../research/litestream-django-scenarios.md) 다.

## 1. 언제 쓰나 / 쓰지 마라

**쓴다**
- 개인 서비스, 사내 도구, 실습 서버, MVP 처럼 서버 한 대로 충분하고, 약 1초 분량의 쓰기 손실(RPO)을 감수할 수 있을 때. 전원 차단을 흉내 낸 측정에서 3회 평균 약 1초(64·78·81건, 0.8~1.1초 분량)를 잃었다(scenarios, RPO).
- 앱이 프로세스 하나로 요청을 감당할 때. 웹소켓 그룹(`group_send`)도 같은 프로세스 안에서만 오가면 된다.
- 볼륨이 남는 플랫폼(VM, Docker 볼륨, Fly 볼륨)과 볼륨 없는 플랫폼(매번 새 컨테이너) 모두.

**쓰지 마라**
- **머신 두 대 이상이 같은 DB 에 쓴다.** 두 머신이 같은 복제본 경로에 replicate 하면 나중에 쓴 쪽이 **경고 없이** 이긴다(scenarios S2b: 160건이 남음). boot 의 잠금은 같은 볼륨 안의 이중 실행만 막고 다른 머신은 막지 못한다([DESIGN §2](../DESIGN.md), §4-2). 블루/그린·롤링 배포도 겹치는 동안 두 쪽이 쓰므로 같은 문제다.
- **워커 프로세스가 둘 이상이다**(`uvicorn --workers 4`, 별도 `runworker` 프로세스 등). `InMemoryChannelLayer` 는 프로세스 안에서만 메시지를 전한다. → [`single-server-multiproc`](single-server-multiproc.md).
- 1초 손실도 안 되는 데이터(결제 등), 다중 쓰기, 수평 확장이 필요하다. Postgres 를 쓴다(scenarios, RPO·판정 7).

## 2. 구성 요소와 프로세스 트리

```text
컨테이너 PID 1
python -m django_sqlite_ops.boot ...        판정·복원·무결성 확인, <db>.boot.lock 잠금
  │ exec (같은 PID, 잠금 fd 상속)
  ▼
litestream replicate -config /etc/litestream.yml -exec "..."     PID 1, 복제
  └─ sh -c 'python manage.py migrate --noinput && exec uvicorn ...'
       │ exec
       ▼
     uvicorn proj.asgi:application --workers 1                    앱 (자식 프로세스 1개)
```

- **boot 는 exec 로 사라진다.** `--` 뒤 명령(`litestream replicate ...`)이 같은 PID 를 이어받는다. `<db>.boot.lock` 잠금은 exec 된 litestream 과 그 자식에게 상속되므로, 앱이 살아 있는 동안 같은 볼륨에서 boot 를 또 돌리면 exit 5 로 막힌다([README boot CLI](../../README.md#boot-cli) '무엇을 하나' 1).
- **`migrate` 는 `-exec` 안에서 돈다.** boot 단계에서 `manage.py` 를 부르면 `django.setup()` 중에 빈 DB 파일이 생겨 복원이 건너뛰어질 수 있다([DESIGN §4-1](../DESIGN.md)). `-exec` 안이면 litestream 이 이미 복제를 시작한 뒤라 마이그레이션도 복제된다. 마이그레이션·VACUUM 을 복제 중에 돌려도 원본과 복원본이 일치했다(scenarios S8, 50,294건).
- **litestream 이 PID 1 이다.** 별도 init(tini 등)이 필요 없었다(docker D1a).
- **종료 순서**: `docker stop` → PID 1(litestream)에 SIGTERM → litestream 이 자식에게 신호를 넘기고 자식이 끝나기를 기다림 → 마지막 sync → 종료. Docker 랩에서 0.4초 만에 exit 0 으로 내려갔고 400건 중 400건이 복제됐다(docker D1a). `docker kill`(SIGKILL)로 쓰는 도중에 죽여도 응답한 1,600건이 로컬 볼륨과 복제본에 모두 있었고, 같은 볼륨으로 다시 띄우면 이어서 서비스했다(docker D1b).
  - D1a·D1b 는 **gunicorn(WSGI)** 으로 잰 값이다. uvicorn(ASGI)은 macOS 호스트에서 `litestream replicate -exec "sh -c 'exec uvicorn ...'"` 의 litestream 에 SIGTERM 을 보내 uvicorn 이 정상 종료하고 litestream 이 `litestream shut down` 으로 끝나는 것만 확인했다(#9). 컨테이너에서의 같은 실측(복제 건수 포함)은 회귀 랩([#10](https://github.com/itda-work/django-sqlite-ops/issues/10), [DESIGN §10](../DESIGN.md))의 몫이다.
  - `-exec` 의 자식이 끝나면 litestream 도 끝난다(`litestream replicate -h`). 앱이 죽으면 컨테이너가 내려가고 재시작 정책이 다시 띄운다. 이때 다시 boot 부터 돈다.
- `stop_grace_period` 는 앱의 graceful 종료 시간보다 넉넉하게 준다. 짧으면 Docker 가 SIGKILL 을 보내 마지막 sync 가 빠질 수 있다(그래도 로컬 볼륨에는 남는다, D1b).

## 3. 설정 조각

경로는 컨테이너 기준 `/data/app.sqlite3`, `/etc/litestream.yml` 이다. 조각마다 검증 범위가 다르다(`tests/test_profiles.py`).

| 조각 | 검증 |
|---|---|
| `settings.py`, `urls.py` | 그대로 실행하고, `check --deploy` 에서 `sqlite_ops.*` 경고가 없음(`channels` 를 import 하지 못하게 막은 상태에서도) |
| `litestream.yml` | 실제 litestream 0.5.17 의 `litestream databases -config` 가 읽음 |
| `entrypoint.sh` | `bash -n`·`sh -n` 문법 검사 |
| `compose.yaml` | YAML 구문만 |
| Dockerfile, `requirements.txt` | 검증 안 함. 컨테이너 종단 검증은 [#10](https://github.com/itda-work/django-sqlite-ops/issues/10) 회귀 랩에서 할 예정 |

### `settings.py`

```python
# settings.py
from django_sqlite_ops.database import sqlite_database

INSTALLED_APPS = [
    # "django.contrib.admin", ... 프로젝트의 앱들
    "django_sqlite_ops",
]

ASGI_APPLICATION = "proj.asgi.application"

# 실제 경로로 쓴다: 부모(/data)가 심볼릭 링크면 boot 가 거부한다 (D-15)
DATABASES = {
    "default": sqlite_database("/data/app.sqlite3", profile="single-server"),
}

SQLITE_OPS = {
    "PROFILE": "single-server",
    "HEALTH": {
        "DATABASES": {"default": {"litestream_config": "/etc/litestream.yml"}},
    },
}

# 프로세스 하나 안에서만 메시지가 오간다
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}
```

- `profile` 은 지금 두 프로필의 DB 설정이 같다. 그래도 배포 형태에 맞는 이름을 쓴다([README 프로필](../../README.md#프로필)). `SQLITE_OPS["PROFILE"]` 은 시스템 체크와 `sqlite_doctor` 의 기준이다.
- `channels` 를 쓰지 않는 앱이면 `CHANNEL_LAYERS` 와 `ASGI_APPLICATION` 은 빼도 된다. `sqlite_doctor` 와 시스템 체크는 `CHANNEL_LAYERS` 를 문자열로만 읽고 `channels` 를 import 하지 않는다.

### `urls.py` — 복제 헬스

```python
# urls.py
from django.urls import path

from django_sqlite_ops.health import health_view

urlpatterns = [
    path("internal/sqlite-health", health_view),
]
```

내부망에만 노출하고, 로드밸런서 헬스 체크로 쓰지 않는다. 응답 형식과 `?strict=1` 은 [README 복제 헬스](../../README.md#복제-헬스).

### `litestream.yml`

```yaml
# /etc/litestream.yml
dbs:
  - path: /data/app.sqlite3        # settings 의 NAME, boot 의 --db 와 같은 실제 경로
    replica:
      type: s3
      bucket: my-app-backups
      path: app                    # 버킷 안 prefix. 오타가 나면 boot 가 '복제본 없음'과 구분하지 못한다
      region: us-east-1
      # S3 호환 서비스(MinIO, SeaweedFS, R2 등)만 쓴다. 스킴(http:// 또는 https://)을 꼭 붙인다
      endpoint: http://s3.internal:8333
      force-path-style: true
```

- 자격 증명은 파일에 쓰지 않고 환경 변수 `LITESTREAM_ACCESS_KEY_ID`·`LITESTREAM_SECRET_ACCESS_KEY`(또는 `AWS_*`)로 준다. Litestream 이 자동으로 읽는다([공식 설정 문서](https://litestream.io/reference/config/)). 설정 파일 안의 `${VAR}` 도 기본으로 펼쳐진다.
- AWS S3 자체를 쓰면 `endpoint`·`force-path-style` 줄을 지운다.
- **`endpoint` 에 스킴이 빠지면** Litestream 은 HTTPS 로 접속해 평문 HTTP 서버 앞에서 무한 대기한다(재현함, CLAUDE.md 함정). boot 는 원격 조회에 `--ltx-timeout`(기본 30초)을 걸어 `remote_error` 로 거부하고, 헬스는 `unknown`(`remote_error`)이 된다.
- `sync-interval` 기본은 `1s` 다. RPO 측정(약 1초 손실)도 이 값이다(scenarios, RPO).

### Dockerfile

Litestream 은 공식 설치 방법(릴리스 `.deb` + `dpkg`, https://litestream.io/install/linux/)으로 넣고, 같은 릴리스의 `checksums.txt` 와 SHA-256 이 맞을 때만 설치한다. CI 의 `litestream` 잡과 같은 방식이다.

> **#10 랩에서 종단 검증 예정.** 이 Dockerfile 은 아직 빌드해 보지 않았다. 설치 단계는 CI 잡의 스크립트를 옮긴 것이다.

```dockerfile
FROM python:3.13-slim

ARG LITESTREAM_VERSION=0.5.17
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64 ;; \
      arm64) arch=arm64 ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    deb="litestream-${LITESTREAM_VERSION}-linux-${arch}.deb"; \
    base="https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}"; \
    cd /tmp; \
    curl -fsSL --retry 3 -o checksums.txt "$base/checksums.txt"; \
    curl -fsSL --retry 3 -o "$deb" "$base/$deb"; \
    want="$(awk -v f="$deb" '$2 == f { print $1 }' checksums.txt)"; \
    test -n "$want" || { echo "no checksum entry for $deb" >&2; exit 1; }; \
    echo "$want  $deb" | sha256sum -c -; \
    dpkg -i "$deb"; \
    test "$(litestream version)" = "$LITESTREAM_VERSION"; \
    rm -f "$deb" checksums.txt; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY litestream.yml /etc/litestream.yml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
```

`requirements.txt` 예(PyPI 배포 전이라 GitHub 아카이브로 받는다. 이미지에 `git` 이 없어도 된다):

```text
django-sqlite-ops @ https://github.com/itda-work/django-sqlite-ops/archive/refs/heads/main.zip
Django>=5.2
channels
uvicorn
```

운영에서는 `main.zip` 대신 커밋 해시 아카이브(`archive/<sha>.zip`)로 고정한다.

### `entrypoint.sh`

```bash
#!/bin/sh
# entrypoint.sh — boot 가 판정·복원한 뒤 litestream 이 앱을 띄우고 복제한다.
# BOOT_FLAGS: 한 번만 쓰는 boot 옵션(--init-new, --adopt-existing, --on-unknown restore).
#             문제를 푼 뒤에는 반드시 비운다.
set -eu
# shellcheck disable=SC2086 # BOOT_FLAGS 는 단어로 나뉘어야 한다
exec python -m django_sqlite_ops.boot \
    --db /data/app.sqlite3 \
    --config /etc/litestream.yml \
    ${BOOT_FLAGS:-} \
    -- litestream replicate -config /etc/litestream.yml \
       -exec "sh -c 'python manage.py migrate --noinput && exec uvicorn proj.asgi:application --host 0.0.0.0 --port 8000 --workers 1'"
```

- `--workers 1` 을 명시한다. uvicorn 은 `--workers` 가 없으면 환경 변수 `WEB_CONCURRENCY` 를 따른다(`uvicorn --help`). 플랫폼이 이 값을 넣으면 모르는 사이에 워커가 늘어 `InMemoryChannelLayer` 가 깨진다.
- 마지막 `exec uvicorn` 으로 `sh` 를 uvicorn 으로 바꾼다. litestream 의 신호가 `sh` 에서 멈추지 않고 uvicorn 에 바로 간다.
- boot 옵션 전체는 [README boot CLI](../../README.md#boot-cli).

### `compose.yaml` (예)

> **#10 랩에서 종단 검증 예정.** YAML 구문만 검사했다.

```yaml
services:
  app:
    build: .
    env_file: .env.litestream      # LITESTREAM_ACCESS_KEY_ID, LITESTREAM_SECRET_ACCESS_KEY
    environment:
      BOOT_FLAGS: ""               # 한 번만 쓰는 boot 옵션. 평소에는 비워 둔다
    ports: ["8000:8000"]
    volumes: ["appdata:/data"]     # 볼륨 없는 무상태 배포면 이 줄을 지운다
    stop_grace_period: 30s
    restart: on-failure
volumes:
  appdata:
```

- 서비스의 컨테이너는 하나만 둔다(`docker compose up --scale app=2` 를 하지 않는다).
- `restart: on-failure` 면 boot 가 거부(exit 2)할 때도 계속 재시작하며 같은 거부를 되풀이한다. 거부는 사람이 판단할 일이므로 로그의 `[boot] decision:` 줄을 보고 아래 절차를 따른다.

## 4. 운영 절차

boot 의 판정·사유 코드·종료 코드는 [README boot CLI](../../README.md#boot-cli) 가 정본이다. 여기서는 이 프로필의 순서만 적는다. `BOOT_FLAGS` 는 위 `entrypoint.sh` 의 환경 변수다.

### 생애 첫 배포 — `--init-new` 를 한 번 쓰고 끈다

1. 볼륨도 복제본도 비어 있으면 boot 는 `no_replica_no_local` 로 거부한다. 복제본 경로·prefix 오타와 "복제본 없음"이 Litestream 출력으로 구분되지 않기 때문이다(D-13).
2. 설정의 `bucket`·`path` 를 다시 확인하고 `BOOT_FLAGS=--init-new` 로 **한 번** 띄운다. boot 는 DB 를 만들지 않고 넘어가고, `migrate` 가 만든다.
3. 첫 복제를 확인한다: 헬스 `status` 가 `caught_up` 이 될 때까지 기다린다(첫 요청 뒤 `REFRESH` 초 이내). 첫 스냅샷이 올라가기 전에 컨테이너를 내리면 복제본이 비어 있을 수 있다(scenarios S2 함정 A: 50건 쓰고 즉시 SIGTERM → 복제본 없음, 3초 뒤면 50/50).
4. **`BOOT_FLAGS` 를 비우고 다시 배포한다.** 켜 둔 채로 두면 나중에 볼륨을 잃고 복제본 경로까지 틀렸을 때 다시 빈 DB 로 시작한다(D-13).

### 기존 DB 도입 — `--adopt-existing` 을 한 번 쓰고 끈다

1. 옛 앱을 멈추고 DB 를 볼륨의 `/data/app.sqlite3` 로 옮긴다. 열린 연결이 있으면 커밋 일부가 `-wal` 에만 있으므로 `-wal`·`-shm` 까지 함께 옮기거나, `sqlite3 old.sqlite3 ".backup /data/app.sqlite3"` 로 한 파일로 만든다.
2. `BOOT_FLAGS=--adopt-existing` 으로 한 번 띄운다. (로컬 DB 있음, 로컬 메타 없음, 복제본 빈 목록) 일 때만 통과한다(D-11).
3. 헬스가 `caught_up` 이 되면 `BOOT_FLAGS` 를 비운다. `--on-unknown keep-local` 을 도입 절차로 쓰지 않는다.

### 무상태 교체 (볼륨 없음)

- 볼륨 없이 새 컨테이너를 띄우면 boot 는 `fresh` 로 판정해 복제본을 복원한다. Docker 랩에서 새 컨테이너가 2.4초 만에 복원하고 200건 중 200건으로 떴다(docker D1c).
- **정지 후 기동**으로 배포한다: 옛 컨테이너를 멈추고(마지막 sync 를 위해 `docker stop`) 새 컨테이너를 띄운다. 두 컨테이너가 겹치면 둘 다 쓴다(§1).
- 볼륨이 없으므로 호스트가 죽으면 마지막 sync 이후의 쓰기를 잃는다(약 1초, scenarios RPO). S3 가 끊긴 동안 쌓인 변경도 그 서버 디스크에만 있다(docker D2: 20초 단절 동안 WAL 8.5MB).

### 옛 볼륨 재부팅이 거부될 때 (`remote_ahead`)

- 볼륨 A 로 쓰다가 다른 볼륨 B 로 이어 쓴 뒤 A 로 다시 부팅하면, 보호가 없을 때는 최신본 150건이 55건으로 덮였다(docker D4, scenarios 함정 B). boot 는 이 경우 복제본이 앞섰다고 보고 거부한다(회귀 랩 L2, [DESIGN §10](../DESIGN.md)).
- 복제본이 정본이 맞으면 `BOOT_FLAGS="--on-unknown restore"` 로 **한 번** 부팅한다. 로컬은 `<db>.stale-<ts>/` 로 격리되고 복제본이 복원된다. 격리본은 boot 가 지우지 않으므로 확인한 뒤 직접 지운다.
- 로컬이 정본이라고 판단되면(복제본 쪽이 실수로 쓰인 경우) 자동 판정할 방법이 없다(D-7). 복제본 상태를 조사한 뒤 사람이 정한다.

### 복원 직후 크래시 (`no_local_meta`)

복원한 DB 옆에는 Litestream 메타가 없고 `litestream replicate` 가 첫 변경을 기록할 때 생긴다. 그 전에 컨테이너가 죽으면 다음 부팅은 `no_local_meta` 로 거부된다. 방금 복제본에서 받은 DB 이므로 `BOOT_FLAGS="--on-unknown restore"` 로 한 번 부팅해 다시 복원한다(D-14).

### 확인 순서

배포마다 이 순서로 본다.

1. `python manage.py check --deploy` — 설정만 보고 DB 를 열지 않는다. 빌드 단계·CI 에서 돌린다. `sqlite_ops.*` 가 없어야 한다.
2. `docker compose exec app python manage.py sqlite_doctor --litestream-config /etc/litestream.yml` — 실제로 연결해 PRAGMA·파일시스템·Litestream 설정·채널 레이어를 본다. exit 0 이어야 한다. 연결하면 `init_command` 가 실행된다([README sqlite_doctor](../../README.md#sqlite_doctor)).
3. `GET /internal/sqlite-health` — 본문 `status` 가 `caught_up`. 배포 직후 첫 응답은 `unknown`(`not_checked`)이고 `REFRESH`(기본 15초) 뒤 다시 본다. 모니터링은 `backlog`·`unknown` 이 몇 분 이어지면 경보한다.

## 5. 함정

- **쓰는 머신은 1대.** 두 머신이 같은 복제본에 쓰면 나중 쪽이 경고 없이 이긴다(scenarios S2b). 배포는 정지 후 기동(§4). 헬스는 복제본이 로컬보다 앞서면 `unknown`(`remote_ahead`)으로 알린다.
- **S3 가 끊겨도 Litestream 은 아무 말이 없다.** 20초 단절 동안 WARN/ERROR 로그 0줄, `litestream status` rc 0, `sync_error_count` 변화 0(docker D3). 헬스 본문 `status` 로 알람을 건다. 서비스 자체는 계속 돈다(docker D2: 단절 중 쓰기 600건 에러 0, 복구 후 약 22초에 따라잡음).
- **`endpoint` 에 `http://` 를 빠뜨리지 않는다**(§3 litestream.yml).
- **`--init-new`·`--adopt-existing`·`--on-unknown` 은 한 번만.** `BOOT_FLAGS` 를 비우는 것까지가 절차다.
- **실제 경로.** `/data` 가 심볼릭 링크면 boot 가 exit 64 로 거부한다. settings 의 `NAME`, boot 의 `--db`, `litestream.yml` 의 `path` 를 같은 실제 경로로 쓴다(D-15).
- **`<db>.boot.lock` 을 지우지 않는다.** 지우면 다음 boot 가 새 파일을 잠가 이중 실행을 막지 못한다.
- **스파이크 엔트리포인트를 쓰지 않는다.** `docs/reference/entrypoint.sh` 의 `restore -if-db-not-exists` 는 0바이트 DB 파일이 있으면 rc 0 으로 복원을 건너뛰고(재현함, DESIGN §4-1), 옛 볼륨을 막지 못한다. boot 가 이 자리를 대신한다.
- **워커는 1개.** `InMemoryChannelLayer` 는 프로세스 밖으로 메시지를 보내지 않는다. `sqlite_doctor` 는 프로필이 `single-server-multiproc` 일 때만 이를 경고하므로, `single-server` 로 두고 워커를 늘리면 경고 없이 깨진다. `WEB_CONCURRENCY` 를 조심한다(§3).
- **migrate 와 옛 코드가 겹치지 않게.** 정지 후 기동이면 겹치지 않는다. 겹치면 새 NOT NULL 컬럼에 옛 코드의 INSERT 가 실패한다(scenarios S8: 6건). 새 NOT NULL 필드에는 `db_default` 를 쓴다.
- **복제는 비동기다.** 커밋 응답이 내구성 보장이 아니다(약 1초 RPO, scenarios RPO).
- **VFS 읽기 복제본은 예정(2단계)이다.** 이 프로필에서 쓰지 않는다([DESIGN §3-2](../DESIGN.md)).
