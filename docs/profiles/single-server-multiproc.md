# 배포 프로필: `single-server-multiproc`

서버(머신) 한 대에 앱 프로세스 여럿. SQLite·Litestream·boot 구성은 [`single-server`](single-server.md) 와 같고, **프로세스 사이 채널 레이어**가 더해진다.

| 항목 | 값 |
|---|---|
| 머신 | 1대 (쓰는 머신은 언제나 1대) |
| 앱 프로세스 | ASGI 서버 워커 여럿 (`uvicorn --workers N`), 필요하면 별도 워커 프로세스 |
| 채널 레이어 | **channels-nats (추천)** · channels_redis (호환, Redis 가 이미 있을 때) |
| 브로커 | nats-server (또는 Redis). 서비스 관리자가 띄운다. 이 패키지는 감독하지 않는다 |
| 복제·부팅 | `single-server` 와 같다 |
| 설정 | `sqlite_database(..., profile="single-server-multiproc")`, `SQLITE_OPS["PROFILE"] = "single-server-multiproc"` |

이 문서는 `single-server` 와 **다른 점만** 적는다. 같은 부분(`litestream.yml`, Dockerfile, 운영 절차 대부분, SQLite 쪽 함정)은 그 문서를 그대로 따른다. 두 문서에 같은 절차를 두 번 쓰면 한쪽만 고쳐지는 일이 생기기 때문이다.

## 1. 언제 쓰나 / 쓰지 마라

**쓴다**
- 한 대의 CPU 코어를 여러 프로세스로 쓰고 싶을 때(`uvicorn --workers N`).
- 웹소켓 그룹(`group_send`)이나 채널 메시지가 **프로세스 사이**를 오가야 할 때. 워커 A 에 붙은 클라이언트에게 워커 B 가 보내는 경우다.

**쓰지 마라**
- **머신 두 대 이상이 같은 DB 에 쓴다.** 프로세스가 여럿이어도 머신은 하나다. 두 머신이 같은 복제본에 replicate 하면 나중 쪽이 경고 없이 이긴다(scenarios S2b, [DESIGN §2](../DESIGN.md)).
- 프로세스가 하나로 충분하다 → [`single-server`](single-server.md). 브로커 하나를 덜 운영한다.

SQLite 쓰기는 프로세스가 여럿이어도 한 번에 하나씩이다. `transaction_mode=IMMEDIATE`·`busy_timeout` 권장값으로 gunicorn 워커 4개·클라이언트 스레드 8개가 1,200건을 쓸 때 `database is locked` 0건, 초당 1,511건, p50 3.2ms, p99 25.8ms 였다(scenarios S2, WSGI). 워커 수는 이 패키지가 정하지 않는다. 쓰기가 많으면 워커를 늘려도 쓰기 처리량은 늘지 않는다.

## 2. 구성 요소와 프로세스 트리

```text
컨테이너 app                                                  컨테이너 nats (또는 systemd 서비스)
PID 1 boot → exec → litestream replicate -exec "..."            nats-server
  └─ sh -c 'migrate && exec uvicorn ... --workers 4'               ▲
       │ exec                                                       │ nats://nats:4222
       ▼                                                            │
     uvicorn (감독 프로세스)                                         │
       ├─ worker 1 ── channels-nats 연결 ───────────────────────────┤
       ├─ worker 2 ── channels-nats 연결 ───────────────────────────┤
       ├─ worker 3 ─ …                                              │
       └─ worker 4 ─ …                                              │
```

- boot·잠금 상속·`migrate` 위치·PID 1 은 [`single-server` §2](single-server.md#2-구성-요소와-프로세스-트리) 와 같다.
- **nats-server 는 `litestream -exec` 아래에 두지 않는다.** `-exec` 는 명령 하나만 띄우고, 그 자식이 끝나면 litestream 도 끝난다(`litestream replicate -h`). 브로커는 별도 컨테이너나 systemd·Windows 서비스로 띄운다. 이 패키지는 nats-server 를 감독하지 않는다([DESIGN §3-3](../DESIGN.md)).
- **종료 순서**: `docker stop` → litestream(PID 1)이 SIGTERM 을 자식(uvicorn 감독 프로세스)에게 넘김 → 감독 프로세스가 워커들을 내림 → litestream 이 마지막 sync 후 종료. Docker 랩에서 gunicorn 워커 구성으로 0.4초, exit 0, 400/400 복제였다(docker D1a). `uvicorn --workers 2` 컨테이너(이 문서의 compose·entrypoint, `WEB_WORKERS=2`)에서 400건을 쓰고 곧바로 `docker stop` 하자 0.72초 만에 exit 0, 복제본 400/400 이었다(앞선 실행 0.84초, 개발 실행 0.48초). 로그 순서: litestream `sending signal to exec process` → 감독 프로세스 `Received SIGTERM, exiting.` → 워커 둘 `Shutting down`·`Finished server process` → `Stopping parent process` → `litestream shut down`(회귀 랩 P2, [`lab-2026-10-08.md`](../research/lab-2026-10-08.md)).
- 앱이 내려가는 동안 브로커는 떠 있어야 워커의 마지막 메시지가 나간다. compose 는 `depends_on` 의 역순으로 서비스를 내린다(의존하는 `app` 이 `nats` 보다 먼저, [Docker 문서](https://docs.docker.com/compose/how-tos/startup-order/)).

## 3. 설정 조각

`litestream.yml`, Dockerfile 은 [`single-server` §3](single-server.md#3-설정-조각) 과 같다(Dockerfile 은 회귀 랩에서 종단 검증함). `requirements.txt` 에 채널 레이어 패키지를 더한다(`channels-nats` 또는 `channels-redis`). 이 패키지의 `[nats]` extra 로 넣어도 된다.

### `settings.py` — channels-nats (추천)

```python
# settings.py
from django_sqlite_ops.database import sqlite_database

INSTALLED_APPS = [
    # "django.contrib.admin", ... 프로젝트의 앱들
    "django_sqlite_ops",
]

ASGI_APPLICATION = "proj.asgi.application"

DATABASES = {
    "default": sqlite_database("/data/app.sqlite3", profile="single-server-multiproc"),
}

SQLITE_OPS = {
    "PROFILE": "single-server-multiproc",
    "HEALTH": {
        "DATABASES": {"default": {"litestream_config": "/etc/litestream.yml"}},
    },
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_nats.NatsChannelLayer",
        "CONFIG": {"servers": ["nats://nats:4222"]},
    },
}
```

설정 키(`prefix`, `expiry`, `capacity`, `connect_deadline` 등)는 [channels-nats README](https://github.com/itda-work/channels-nats#설정) 가 정본이다.

### `settings.py` — channels_redis (호환, Redis 가 이미 있을 때)

```python
# settings.py — CHANNEL_LAYERS 만 다르다
from django_sqlite_ops.database import sqlite_database

INSTALLED_APPS = ["django_sqlite_ops"]

DATABASES = {
    "default": sqlite_database("/data/app.sqlite3", profile="single-server-multiproc"),
}

SQLITE_OPS = {
    "PROFILE": "single-server-multiproc",
    "HEALTH": {
        "DATABASES": {"default": {"litestream_config": "/etc/litestream.yml"}},
    },
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [("redis", 6379)]},
    },
}
```

`channels_redis.pubsub.RedisPubSubChannelLayer` 는 의미론을 재지 않아 `sqlite_doctor` 가 `unknown` 으로 적는다. 호환 레이어로는 `RedisChannelLayer` 를 쓴다.

`urls.py`(헬스)는 [`single-server` §3](single-server.md#urlspy--복제-헬스) 과 같다. 헬스 스레드는 워커마다 따로 돈다. 워커별 응답이 몇 초 다를 수 있다([README 복제 헬스](../../README.md#복제-헬스)).

### `entrypoint.sh`

```bash
#!/bin/sh
# entrypoint.sh — single-server 와 같고 --workers 만 다르다.
set -eu
# shellcheck disable=SC2086 # BOOT_FLAGS 는 단어로 나뉘어야 한다
exec python -m django_sqlite_ops.boot \
    --db /data/app.sqlite3 \
    --config /etc/litestream.yml \
    ${BOOT_FLAGS:-} \
    -- litestream replicate -config /etc/litestream.yml \
       -exec "sh -c 'python manage.py migrate --noinput && exec uvicorn proj.asgi:application --host 0.0.0.0 --port 8000 --workers ${WEB_WORKERS:-4}'"
```

`${WEB_WORKERS:-4}` 는 entrypoint 의 `sh` 가 펼친다(큰따옴표 안). litestream 은 펼쳐진 숫자를 받는다.

### `compose.yaml` (예)

> **종단 검증함**(회귀 랩 P2): 이 파일 그대로에 이미지 이름·라벨·호스트 포트를 덮고 `WEB_WORKERS=2`·nats 이미지 태그 고정으로 띄웠다. 앱이 워커 2개(서로 다른 PID)로 응답하고, `sqlite_doctor` 가 exit 0·`channels_nats.NatsChannelLayer` OK 였다. 채널 레이어를 런타임에 쓰는 검증(channels-nats 설치·메시지 전달)은 하지 않았다. 두 `settings.py` 조각과 `entrypoint.sh` 의 검증 범위는 [`single-server` §3](single-server.md#3-설정-조각) 표와 같다.

```yaml
services:
  nats:
    image: nats:2                  # 공식 이미지. 4222 클라이언트 포트
    restart: unless-stopped
  app:
    build: .
    env_file: .env.litestream      # LITESTREAM_ACCESS_KEY_ID, LITESTREAM_SECRET_ACCESS_KEY
    environment:
      BOOT_FLAGS: ""               # 한 번만 쓰는 boot 옵션. 평소에는 비워 둔다
      WEB_WORKERS: "4"
    ports: ["8000:8000"]
    volumes: ["appdata:/data"]
    depends_on: [nats]
    stop_grace_period: 30s
    restart: on-failure
volumes:
  appdata:
```

- `app` 서비스의 컨테이너는 하나만 둔다. 프로세스를 늘릴 때는 `WEB_WORKERS` 를 올리고 컨테이너 수(`--scale`)를 늘리지 않는다. 같은 볼륨을 두 컨테이너가 쓰면 두 번째 boot 가 잠금(exit 5)에서 막히고, 다른 볼륨이면 두 머신이 쓰는 것과 같다.
- nats-server 에 인증·TLS 를 걸면 channels-nats 의 `connect_options` 로 넘긴다. 이 패키지는 NATS 설정을 검사하지 않는다.

### 채널 레이어 의미론 ([DESIGN §8](../DESIGN.md))

추천을 정했다고 레이어 사이의 차이가 사라지지는 않는다. 바꾸기 전에 이 표를 본다.

| | channels-nats | channels_redis |
|---|---|---|
| 수신자 생성 전에 보낸 메시지 | 사라짐(실측 0/5) | 보관됨(만료까지) |
| 순서 보장 | 구독 하나 안에서만 | 채널 단위 |
| `ChannelFull` | 발생하지 않음 | 발생함 |
| 디스크 쓰기 / Litestream 영향 | 없음 | 없음(Redis 쪽) |
| 1:1 지연 p50 (로컬 실측) | 0.3ms | (이번에는 재지 않음) |

- 실측 출처: `docs/research/sqlite-django-package-review-v0.md`(0/5, p50 0.3ms). channels-nats 의 버퍼 위치·드롭 층·취소 공백은 [channels-nats README 'Channels 규약과 다른 점'](https://github.com/itda-work/channels-nats#channels-규약과-다른-점) 에 있다.
- 일반 컨슈머는 연결할 때 구독하므로 '수신자 전 메시지' 차이를 겪지 않는다. 임의 이름 채널에 먼저 `send` 하고 나중에 `receive` 하는 코드, `ChannelFull` 을 잡던 코드는 channels-nats 에서 동작이 다르다.
- channels-nats README 의 위상 절은 지금 "새 프로젝트이고 특별한 이유가 없으면 channels_redis 를 먼저 검토"라고 쓴다. 이 저장소의 추천(D-2)과 맞추는 문구 조정은 OSS PO 가 진행한다(D-3). 이 저장소에서 그 문서를 고치지 않는다.

### channels-lite 를 쓴다면

SQLite 기반 채널 레이어(channels-lite)는 **추천하지 않는다**. 폴링 간격과 유휴 CPU 를 맞바꿔야 하고(0.1초 간격이면 p50 51ms, 0.001초면 p50 2.3ms·유휴 CPU 코어의 약 19%), 메시지마다 DB 쓰기가 2번 생긴다(`docs/research/sqlite-django-package-review-v1.md`, [DESIGN §8](../DESIGN.md)). 그래도 쓴다면:

- 채널 DB 는 앱 DB 와 **별도 파일**로 두고, `litestream.yml` 의 복제 대상에서 **뺀다**. `sqlite_doctor` 가 둘 다 검사한다.
- aio 레이어(`AIOSQLiteChannelLayer`)면 중복 배달 패치를 켠다: `SQLITE_OPS["PATCH_CHANNELS_LITE_AIO"] = True`([README channels-lite 패치](../../README.md#channels-lite-패치), #8).
- 새 채널 DB 는 배포 때(`migrate` 직후) 미리 `PRAGMA journal_mode=WAL` 로 바꿔 둔다. 여러 프로세스가 새 DB 에 동시에 처음 WAL 을 걸면 일부가 `database is locked` 로 실패할 수 있다([README channels-lite 패치 '함정'](../../README.md#channels-lite-패치)).

## 4. 운영 절차

생애 첫 배포(`--init-new`), 기존 DB 도입(`--adopt-existing`), 무상태 교체, 옛 볼륨 재부팅 거부(`remote_ahead`), 복원 직후 크래시(`no_local_meta`)는 [`single-server` §4](single-server.md#4-운영-절차) 와 같다. 다른 점:

- **브로커를 먼저 띄운다.** nats-server 가 없으면 HTTP 요청과 DB 는 정상이지만 채널 레이어 호출이 실패한다(channels-nats 기본 `connect_deadline` 5초 뒤 `NoServersError`, [channels-nats README](https://github.com/itda-work/channels-nats#설정)). 브로커 재시작은 DB·복제와 무관하므로 boot 를 다시 거치지 않는다.
- **배포는 정지 후 기동.** 옛 컨테이너의 워커가 모두 내려간 뒤 새 컨테이너를 띄운다. 워커가 여럿이어도 SQLite 를 쓰는 머신은 하나다.

### 확인 순서

1. `python manage.py check --deploy` — `sqlite_ops.*` 가 없어야 한다.
2. `docker compose exec app python manage.py sqlite_doctor --litestream-config /etc/litestream.yml` — exit 0. `[channels]` 섹션에 `channels_nats.NatsChannelLayer` 가 `OK` 로 나온다. 프로필이 `single-server-multiproc` 인데 `InMemoryChannelLayer` 면 `WARN` 이다. doctor 는 브로커에 접속하지 않는다.
3. `GET /internal/sqlite-health` — 본문 `status` 가 `caught_up`. 워커마다 첫 요청 때 스레드가 시작되므로 처음 몇 번은 `unknown`(`not_checked`)일 수 있다.

## 5. 함정

[`single-server` §5](single-server.md#5-함정) 의 SQLite·Litestream 함정(쓰는 머신 1대, S3 단절의 침묵, `endpoint` 스킴, 한 번만 쓰는 boot 옵션, 실제 경로, 잠금 파일, 비동기 복제)이 모두 해당한다. 이 프로필에서 더해지는 것:

- **`InMemoryChannelLayer` 를 쓰지 않는다.** 워커 사이로 메시지가 가지 않는다. 프로필을 `single-server-multiproc` 로 두면 `sqlite_doctor` 가 경고한다.
- **`SQLITE_OPS["PROFILE"]` 을 실제 배포 형태와 맞춘다.** 프로필을 `single-server` 로 둔 채 워커를 늘리면 위 경고가 나오지 않는다.
- **channels-nats 는 수신자가 생기기 전의 메시지를 버린다.** `ChannelFull` 도 나지 않는다(§3 의미론 표). 이 차이에 기대는 코드는 channels_redis 가 맞다.
- **브로커를 앱과 같은 `-exec` 에 넣지 않는다**(§2).
- **컨테이너를 늘려 확장하지 않는다.** 워커 수로 늘린다(§3 compose).
- **VFS 읽기 복제본은 예정(2단계)이다**([DESIGN §3-2](../DESIGN.md)).
