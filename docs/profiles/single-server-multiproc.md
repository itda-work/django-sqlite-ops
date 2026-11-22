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

`litestream.yml`, Dockerfile 은 [`single-server` §3](single-server.md#3-설정-조각) 과 같다(Dockerfile 은 회귀 랩에서 종단 검증함). `requirements.txt` 에 채널 레이어 패키지를 더한다(`channels-nats` 또는 `channels-redis`). 이 패키지의 `[nats]` extra 로 넣어도 된다. channels-nats 는 `channels-nats>=0.10.1` 로 적는다 — 0.10.0 이하는 구독 확인이 취소되면 그 워커의 연결이 읽기를 멈춘다(§5). 0.1.0a2 부터 `[nats]` extra 가 이 하한을 요구한다(#91). 0.1.0a1 을 쓴다면 `[nats]` 에 하한이 없으므로 함께 적는다.

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

> **종단 검증함**(회귀 랩 P2): 이 파일 그대로에 이미지 이름·라벨·호스트 포트를 덮고 `WEB_WORKERS=2`·nats 이미지 태그 고정으로 띄웠다. 앱이 워커 2개(서로 다른 PID)로 응답하고, `sqlite_doctor` 가 exit 0·`channels_nats.NatsChannelLayer` OK 였다. **정정(2026-10-26)**: 2026-10-08 의 이 검증은 nats 컨테이너 없이 돌았다 — 랩이 앱을 `--no-deps` 로 띄워 `depends_on` 이 무시됐고, channels-nats 를 설치하지 않아 아무것도 실패하지 않았다(#66 에서 재현). 2026-10-26 부터는 nats 를 먼저 띄우고 channels-nats(`[nats]` extra, 2026-10-26 0.10.0 · 2026-10-27 부터 0.10.1)를 넣은 이미지로 워커 사이 전달을 확인한다: 그룹·전용 채널 워커마다 200/200, 일반 채널 큐 그룹 합 200·중복 0, 수신 루프를 남긴 채 stop exit 0·0.74초([`lab-2026-10-26-multiproc-nats.md`](../research/lab-2026-10-26-multiproc-nats.md)). 두 `settings.py` 조각과 `entrypoint.sh` 의 검증 범위는 [`single-server` §3](single-server.md#3-설정-조각) 표와 같다.

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

- `app` 서비스의 컨테이너는 하나만 둔다. 프로세스를 늘릴 때는 `WEB_WORKERS` 를 올리고 컨테이너 수(`--scale`)를 늘리지 않는다. 같은 볼륨을 두 컨테이너가 쓰면 두 번째 boot 가 잠금(exit 5)에서 막히고, 다른 볼륨이면 두 머신이 쓰는 것과 같다. boot 를 돌리지 않고 같은 볼륨의 DB 만 여는 다른 서비스는 §4 「[boot 밖에서 DB 를 여는 컨테이너](#boot-밖에서-db-를-여는-컨테이너)」를 따른다.
- nats-server 에 인증·TLS 를 걸면 channels-nats 의 `connect_options` 로 넘긴다. 이 패키지는 NATS 설정을 검사하지 않는다.
- 열린 파일 한도(`ulimits`)는 [`single-server`](single-server.md#composeyaml-예) 와 같은 이유로 넣지 않았다([이유](fd-limits.md#6-프로필-compose-예에-ulimits-를-넣지-않은-이유)). 한도는 워커 프로세스마다 따로 적용되므로([getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)) [계산](fd-limits.md#4-얼마로-둘까--계산법)은 워커 하나가 맡는 동시 접속 수로 한다.

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
- 워커 사이 손실은 계약대로 두 곳에서만 재현됐다(랩 P2, 2026-10-26): 일반 채널을 `wait_for` 로 폴링할 때 두 폴링 사이(10ms 폴링 184/200), 받는 쪽 mailbox 가 `capacity`(기본 100)를 넘칠 때(간격 없는 200건 버스트). 그룹·프로세스 전용 채널은 1ms 취소를 수천 번 겪어도 200/200 이었다.
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

### boot 밖에서 DB 를 여는 컨테이너

웹과 워커·MCP 서버를 컨테이너로 나누고 DB 볼륨 하나를 함께 쓰는 형태다(예: boot·`replicate` 를 돌리는 `web` + 같은 볼륨의 DB 를 여는 `mcp`). 같은 호스트의 같은 로컬 볼륨이면 쓰는 머신은 여전히 1대라 SQLite 전제가 깨지지 않는다. 다만 §2 의 프로세스 트리(`boot → litestream → 앱`) 밖에 프로세스가 생기므로 다음 규칙을 지킨다.

- **boot 와 `litestream replicate` 는 한 컨테이너에서만 돈다.** 다른 컨테이너는 boot 를 돌리지 않고 앱 명령만 띄운다. 다른 컨테이너에서도 boot 를 돌리면 잠금에서 exit 5 로 막힌다(§3 compose 의 `--scale` 과 같은 경우). 거꾸로 boot 를 돌리지 않는 컨테이너는 `<db>.boot.lock` 을 잡지 않으므로 아무 데도 걸리지 않는다 — boot 가 그 컨테이너를 알아차리지 못한다는 뜻이다.
- **다른 컨테이너는 boot 컨테이너의 앱이 준비된 뒤에 띄운다.** 판정되지 않은 DB 를 열지 않기 위해서다. 복원 전에 열면 빈 DB 파일이 생긴다([DESIGN §4-1](../DESIGN.md#4-1-왜-django-관리-명령이-아닌가)) — boot 는 그 파일을 `no_local_meta` 로 거부한다. 기준은 boot 컨테이너의 앱이 `migrate` 를 마치고 실제로 요청에 응답하는 것(readiness)이다. boot 로그의 `[boot] exec:` 는 boot 의 판정·조치가 끝났다는 진단 로그일 뿐 준비 신호가 아니다: exec 직전에 찍히므로 명령 실행의 성공도 보장하지 않고(exec 가 실패하면 exit 127), 그 뒤에 Litestream 시작 → `migrate` → uvicorn 기동이 남아 있다(§3 `entrypoint.sh`, 코드상 확인). compose 라면 boot 컨테이너에 앱의 응답을 보는 `healthcheck` 를 두고, 다른 서비스의 `depends_on` 에 `condition: service_healthy` 를 건다. `depends_on` 만으로는 시작 순서만 정해지고 준비 완료는 기다리지 않는다([Docker 문서](https://docs.docker.com/compose/how-tos/startup-order/)). 이 프로필의 Dockerfile 에 그 검사용 도구는 정해 두지 않았다 — 이미지에 있는 것으로 쓴다.
- **복원이 걸린 기동은 DB 를 여는 모든 컨테이너를 내린 뒤에 한다.** `--on-unknown restore`(`remote_ahead`·`no_local_meta`·`stale_meta` 등의 대처), 종료 코드 3 복구에서 DB 파일을 옮기는 일, 볼륨을 비운 뒤의 복원(`fresh`), **중단된 격리의 재개**가 여기 든다. 앞선 부팅이 격리 도중 죽어 `<db>.stale-<ts>.partial/` 이 남았으면, 다음 부팅은 옵션이 없어도 판정 전에 그 격리를 마저 끝내며 DB 를 옮긴다([DESIGN §4-2](../DESIGN.md#4-2-실행-흐름) 2단계). 그래서 복구 도중 boot 가 실패했다면(예: 종료 코드 4) 다음 부팅에서 옵션을 뺐더라도 다른 컨테이너를 계속 내려 둔다. 격리 도중 죽은 부팅은 `.partial` 과 함께 그 부팅이 먼저 받아 둔 `<db>.restore-…/` 도 남긴다(boot 는 복원·검증을 마친 뒤 격리하고 설치한다, 코드상 확인). 그러면 재개한 부팅은 DB 를 옮긴 뒤 복원하지 않고 `leftover restore output` 으로 exit 4 에서 멈추고([README 종료 코드](../../README.md#종료-코드) 4, [D-30](../DECISIONS.md)), 남은 `<db>.restore-*` 를 치울 때까지 **운영 경로에 DB 가 없다.** 그 사이 다른 컨테이너가 DB 를 열면 SQLite 가 0바이트 파일을 새로 만들고(표가 없어 쓰기는 실패한다), 산출물을 치운 다음 부팅도 그 파일 때문에 복원하지 않고 `no_local_meta`(`local db is 0 bytes`)로 거부한다 — 대처는 [README 0바이트 DB 복구](../../README.md#0바이트-db-복구-empty_db)의 '메타가 없을 때'다. 다른 컨테이너를 내려 두어야 하는 이유가 하나 더 있는 셈이다. boot 가 exec 까지 가고 앱이 준비된 뒤(위 healthcheck)에 다른 컨테이너를 띄운다. boot 는 로컬 DB·`-wal`·`-shm` 을 rename 으로 `<db>.stale-<ts>/` 에 옮기고 복원본을 제자리에 놓는데, 다른 프로세스가 이미 열어 둔 연결은 옮겨진 파일을 계속 쥔다. 그 연결로 한 쓰기는 **오류 없이 성공하고 격리본에만 남는다.** 복제되지 않고 로그도 없다. 같은 프로세스가 새로 연 연결은 운영 경로의 복원본에 쓰므로, 한 프로세스 안에서 두 연결이 서로 다른 DB 를 본다. 연결을 오래 쥐는 프로세스(`CONN_MAX_AGE=None`, 상주 워커)일수록 오래 간다.
  - 재현함: 가짜 litestream + 실제 boot CLI + SQLite 3.53.1, macOS(`tests/test_boot_cli.py` 의 `test_open_connection_from_another_process_keeps_writing_to_the_quarantined_db`, 중단된 격리를 옵션 없이 재개하는 경우는 `test_resuming_an_interrupted_quarantine_without_options_also_moves_an_open_db` — 실제 부팅을 격리 rename 직전에 죽여 `.partial`·`<db>.restore-*` 를 남긴 뒤, 재개 부팅의 exit 4, 다른 프로세스의 새 연결이 만든 0바이트 파일, 산출물을 치운 뒤의 `no_local_meta` 거부까지 본다. CI 에서는 Linux 에서도 돈다, #133). 실제 Litestream 과 Docker 볼륨을 함께 쓰는 두 컨테이너에서는 **미검증**이다.
  - compose 라면 `docker compose stop` 으로 전체를 내리고 boot 컨테이너만 `BOOT_FLAGS` 를 주어 띄운 뒤, 앱이 준비되면(위 healthcheck) 다른 컨테이너를 띄운다. boot 컨테이너만 `--on-unknown restore` 로 다시 띄우면 다른 컨테이너는 그대로 돌고 있다. `depends_on` 의 `restart: true` 는 의존 서비스를 다시 시작할 뿐 복원 동안 내려가 있음을 보장하지 않으므로 이 규칙을 대신하지 않는다.
- **정상 운용 상태의 평소 재시작은 boot 컨테이너만 다시 띄워도 된다.** 조건은 둘이다: boot 옵션이 없고, 중단된 격리(`<db>.stale-<ts>.partial/`)가 남아 있지 않다(앞선 부팅이 exec 까지 갔다). 그러면 DB 가 있을 때 기본 정책(`refuse`)의 조치는 진행(`match`)이나 거부뿐이라 boot 가 파일을 옮기지 않는다([DESIGN §4-3](../DESIGN.md#4-3-판정-상태-4가지) 조치 규칙, 코드상 확인). `.partial` 이 있으면 위 「복원이 걸린 기동」이다. 첫 실사용처에서도 다른 컨테이너가 도는 채 boot 컨테이너만 재시작해 `match` 로 진행했고 이후 정상이었다(사용처 실측, 이 저장소에서는 재현하지 않음).
- **같은 호스트의 같은 로컬 볼륨이어야 한다.** 다른 호스트에서 네트워크 파일시스템으로 붙이면 쓰는 머신이 둘이 되고 SQLite 잠금도 믿을 수 없다. `sqlite_doctor` 의 `[mount]` 가 네트워크 파일시스템을 `WARN` 으로 알린다.
- **다른 컨테이너에서 부르는 진단의 뜻**(코드상 확인):
  - 헬스 스레드는 프로세스마다 돌고, 공유 볼륨의 로컬 메타·DB 파일을 읽은 뒤 `litestream ltx` 를 직접 부른다. 같은 `settings.py` 의 `SQLITE_OPS["HEALTH"]` 를 쓰는 컨테이너에 litestream 바이너리·설정 파일·복제본 자격 증명이 없으면 원격 조회가 실패해 그 컨테이너의 헬스는 `unknown`(`remote_error`)이다. 바이너리·설정 파일이 없을 때는 `tests/test_health.py` 의 `test_view_real_lookup_failure_is_unknown_with_200` 이 본다. 자격 증명만 없을 때는 실행하지 않았다. 헬스 검사는 boot 컨테이너로 보내거나, 다른 컨테이너에도 셋을 넣는다.
  - `sqlite_doctor --litestream-config` 의 `[litestream]` 은 `litestream databases -config` 를 부르므로 그 컨테이너에 바이너리와 설정 파일이 있어야 한다(자격 증명은 쓰지 않는다). 없으면 `error` 다(`tests/test_doctor.py` 의 `test_litestream_config_missing_is_error`·`test_litestream_binary_missing_is_error`).
  - `[process]` 의 `nofile` 은 doctor 를 부른 그 프로세스의 한도다. 컨테이너마다 따로 확인한다([fd 한도 가이드](fd-limits.md)).

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
- **한 번에 `capacity`(기본 100)보다 많이 몰아 보내면 받는 쪽이 버린다.** 경고는 60초에 한 번이라 첫 줄("1 message(s) dropped so far")만 보면 손실을 과소평가한다. 합계는 "has room again after dropping N" 줄이다(재현함, #66).
- **channels-nats 는 0.10.1 이상을 쓴다.** 0.10.0 이하에서는 구독 확인이 취소되거나 시간 초과되면 그 워커의 레이어 연결이 읽기를 멈춘다. 구독 확인은 `new_channel`·`group_add`·일반 채널 첫 `receive` 가 기다리는 것이다. nats-py 2.16.0 의 읽기 루프가 끝나 그 연결은 아무것도 받지 못하고(로그 `nats: encountered error … InvalidStateError`), 그 뒤의 `new_channel`·`group_add` 도 예외 없이 성공한다.
  - 0.10.0 에서 일반 채널을 `wait_for` 로 폴링하면 1ms 는 매번, 10ms 도 6회 중 1회 재현했다(랩 RTT 약 0.25ms, 2026-10-26, #66). nats-py 의 flush 타임아웃(10초)과 레이어 `publish_deadline`(10초)도 같은 경로다(브로커가 그보다 오래 멈췄다 살아날 때).
  - 0.10.1 은 nats-py 2.16.0 이하일 때 상류 수정(nats-py PR #1031, 병합·미릴리스)과 같은 패치를 넣는다. 랩 P2 에서 1ms·10ms 폴링 뒤에도 같은 연결이 그룹 메시지를 200/200 받았다(재현함, #88, 2026-10-27). 세 경로는 channels-nats 저장소의 회귀 테스트가 본다.
  - 0.10.1 에서도 일반 채널 폴링은 두 폴링 사이의 메시지를 잃는다(계약). 일반 채널은 폴링하지 말고 계속 `receive` 한다.
  - 경위는 [이관 문서](../research/handoff-marco-nats-flush-cancel.md).
- **컨테이너를 늘려 확장하지 않는다.** 워커 수로 늘린다(§3 compose).
- **boot 를 돌리지 않고 같은 볼륨의 DB 를 여는 컨테이너가 있으면, 복원이 걸린 기동 전에 그것부터 내린다.** 열린 연결의 쓰기는 격리본으로 가고 오류가 나지 않는다([boot 밖에서 DB 를 여는 컨테이너](#boot-밖에서-db-를-여는-컨테이너), #133).
- **열린 파일 한도는 워커마다 따로다.** 컨테이너에 준 `nofile` 을 워커 프로세스가 각각 물려받는다. 설정·확인·계산은 [fd 한도 가이드](fd-limits.md).
- **VFS 읽기 복제본은 예정(2단계)이다**([DESIGN §3-2](../DESIGN.md)).
