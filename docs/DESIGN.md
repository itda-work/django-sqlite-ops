# django-sqlite-ops 설계서 v1

- 상태: 설계 확정 전 단계(v1). 구현은 아직 없다.
- 작성: Django전문가-아이스버그(itda-django), 2026-10-07
- 근거: `docs/research/` 의 보고서 6편과 교차 리뷰 원문. 수치는 모두 그 보고서의 실측이다.
- 이름 `django-sqlite-ops` 는 가칭이다. 공개 전에 바꿀 수 있다(→ `docs/DECISIONS.md` D-6).

---

## 1. 한 줄 정의

Django 프로젝트가 **SQLite 를 운영 DB 로 안전하게 쓰게 하는 운영 도구 패키지**다. 설정을 만들어 주는 패키지가 아니다. 부팅 순서, 복제 상태 확인, 진단, 배포 프로필을 맡는다.

## 2. 왜 운영인가 (설정은 이미 해결됨)

- **설정은 Django 가 이미 지원한다.** Django 5.1 부터 `OPTIONS` 에 `init_command`(PRAGMA)와 `transaction_mode` 가 들어왔다. dj-lite(116줄)는 이 dict 를 만들어 주는 정도다. 그래서 이 패키지는 설정 생성기를 만들지 않고, 표준 `DATABASES` 를 **읽기만** 한다.
- **비어 있는 칸은 운영이다.** Litestream 실측(`docs/research/litestream-django-*.md`)에서 드러난 사고는 모두 운영 경계에서 났다.
  - 옛 디스크로 재부팅하면 복제본이 **경고 없이** 덮어써진다(150행 → 55행).
  - S3 가 20초 끊긴 동안 WARN/ERROR 로그가 0줄이었다. `litestream status` 는 rc 0 을 돌려준다.
  - 두 머신이 동시에 replicate 하면 나중에 쓴 쪽이 경고 없이 이긴다.
  - VFS 읽기 복제본은 `CONN_MAX_AGE=0` 이면 요청마다 1,008ms 가 걸리고, `None` 이면 1.7ms 다(WSGI 기준).
  - Linux 에서 VFS 확장을 로드하면 그 뒤의 일반 `connect` 가 `automatic extension loading failed` 로 실패한다.

## 3. 범위

### 3-1. v0.1 에 넣는 것
1. **boot CLI** (`python -m django_sqlite_ops.boot`): 복원 판정 → 복원 → 무결성 확인 → 잠금 → 다음 명령 exec. **Django 를 import 하지 않는다.** (§4)
2. **`sqlite_doctor` 관리 명령**: DB 에 실제로 연결해서 하는 진단. 명시적으로 실행해야만 돈다. (§6)
3. **정적 시스템 체크** 소수: 설정만 보고 DB 는 열지 않는다. (§6)
4. **헬스 뷰**: 복제 상태를 `caught_up / backlog / unknown` 으로 보고한다. (§7)
5. **배포 프로필 문서**: `single-server`, `single-server-multiproc`. (§8)
6. **회귀 랩**: Docker(SeaweedFS + toxiproxy)로 실패 경계를 재현한다. (§10)

### 3-2. 2단계 이후
- VFS 읽기 복제본용 DB 라우터(옵션)
- 테넌트별 DB 보조
- 자동 최신본 판정. "로컬과 원격이 같은 이력"을 증명하는 방법이 정해진 뒤에만 넣는다.

### 3-3. 하지 않는 것
- 자체 채널 레이어. 채널 레이어는 channels-nats 를 추천하고, channels_redis 는 호환 레이어로 둔다(§8).
- 설정 생성기, DB 백엔드 교체·래핑.
- Litestream writable VFS. 상류 #1271, #1363 이 열려 있고, 실측에서 `database disk image is malformed` 가 났다.
- nats-server 프로세스 감독. 서비스 관리(systemd·Windows 서비스)의 몫이다.
- Turso / libSQL. Django 백엔드에 `create_function` 공백이 남아 있다(`docs/research/turso-django.md`).

## 4. boot CLI — 핵심 기능

### 4-1. 왜 Django 관리 명령이 아닌가
`manage.py <cmd>` 는 `handle()` 에 들어가기 전에 `django.setup()` 과 system checks 를 돈다. 그 사이에 앱 `ready()` 나 체크가 DB 연결을 열면 **빈 DB 파일이 생긴다.** 그러면 이어지는 `litestream restore -if-db-not-exists` 가 "DB 가 이미 있다"고 보고 복원을 건너뛴다. 그래서 복원 이전 단계는 Django 를 import 하지 않는 독립 CLI 로 둔다.

### 4-2. 실행 흐름
```
python -m django_sqlite_ops.boot \
    --db /data/app.sqlite3 --config /etc/litestream.yml \
    [--on-unknown refuse|restore|keep-local]   # 기본 refuse
    -- <다음 명령...>                          # 예: litestream replicate -exec "uvicorn ..."
```
1. **잠금 획득**: `<db>.boot.lock` (OS 파일 잠금). 같은 볼륨에서 두 번 부팅하는 것을 막는다. 다른 머신 사이의 이중 replicate 는 막지 못한다. 이는 문서와 `sqlite_doctor` 경고로 다룬다.
2. **판정** (§4-3)
3. **조치**: 판정 결과에 따라 복원하거나, 그대로 두거나, 거부한다.
4. **무결성 확인**: `PRAGMA quick_check` 결과가 `ok` 가 아니면 exit 3 으로 끝낸다.
5. **exec**: `--` 뒤의 명령으로 프로세스를 교체한다. `migrate` 는 그 명령 안에서(또는 운영자가 따로) 실행한다.

### 4-3. 판정 (상태 3가지)
| 상태 | 조건 | 기본 동작 |
|---|---|---|
| `fresh` | 로컬 DB 파일이 없음 | 원격에 복제본이 있으면 복원, 없으면 그대로 진행(새 DB) |
| `match` | 로컬 메타의 최대 TXID ≥ 원격 최대 TXID, 그리고 원격 조회 성공 | 그대로 진행 |
| `unknown` | 그 밖의 모든 경우: 원격 조회 실패·타임아웃, 로컬 메타 없음, 원격이 앞섬, 파싱 실패 | **기동 거부(exit 2)**. 사유를 한 줄로 출력 |

- `--on-unknown restore` : 로컬을 `<db>.stale-<ts>/` 디렉터리 하나로 **원자적으로** 옮긴 뒤 복원한다. 옮기기는 디렉터리 rename 한 번으로 한다. 파일을 하나씩 옮기면 중간에 죽었을 때 반쯤 옮겨진 상태가 남는다.
- `--on-unknown keep-local` : 로컬을 그대로 두고 진행하되, stderr 와 헬스 상태에 `unknown_at_boot` 를 남긴다.
- **원격 TXID 조회**는 `litestream ltx -level all <url>` 로 모든 레벨을 본다. 기본은 L0 만 나열한다(0.5.17 `ltx -h` 로 확인).
- 판정에 쓰는 정보와 비교 규칙은 `boot/decide.py` 의 순수 함수 하나에 모은다. 입력은 (로컬 존재 여부, 로컬 메타 TXID|None, 원격 조회 결과|오류)이고 출력은 (상태, 사유)다. 표 기반 단위 테스트로 모든 조합을 고정한다.

### 4-4. 스파이크 guard.py 와의 차이 (고친 결함)
`docs/reference/guard_spike.py` 는 랩에서 D4(옛 볼륨 재부팅)를 막는 데 성공했다. 그러나 그대로 옮기면 안 된다. 교차 리뷰에서 지적됐고, 코드로 직접 확인한 결함이다.
- 원격 조회 실패와 빈 목록을 둘 다 "로컬 유지, exit 0"으로 처리한다 → v1 에서는 `unknown` 으로 판정한다.
- 메타가 없으면 원격으로 복원한다. 그러면 더 새로운 로컬을 버릴 수 있다 → v1 에서는 `unknown` 으로 판정한다.
- 격리를 파일별 rename 으로 한다 → v1 은 디렉터리 하나로 원자적으로 옮긴다.
- L0 만 본다 → v1 은 `-level all` 을 쓴다.

### 4-5. 종료 코드
`0` 진행(exec) · `2` unknown 거부 · `3` 무결성 실패 · `4` 복원 실패 · `5` 잠금 실패 · `64` 사용법 오류

## 5. Django 쪽 구성

```
django_sqlite_ops/
├── boot/            Django import 금지. 표준 라이브러리 + subprocess(litestream)만
│   ├── __main__.py
│   ├── decide.py    판정 순수 함수
│   ├── litestream.py  ltx/restore 호출과 출력 파싱(버전 고정 테스트)
│   └── lock.py
├── apps.py          체크 등록만. ready()에서 DB 를 열지 않는다
├── checks.py        정적 체크 (§6)
├── management/commands/sqlite_doctor.py
├── health.py        헬스 상태 계산 + 뷰 (§7)
├── compat/
│   └── channels_lite.py  선택적 몽키패치 (§9)
└── vfs.py           (2단계) 확장 로드 우회 — 옵션
```
- 의존성: Django ≥ 5.2(LTS)만 필수다. extra 로 `[nats]` → channels-nats, `[channels-lite]` → channels-lite 를 둔다.
- 지원 범위(초안): Python 3.13+, Django 5.2 / 6.1, SQLite 3.37+ (Django 하한). Litestream 0.5.17 에 맞추고, CLI 출력 파싱은 버전 범위를 명시한다.

## 6. 진단과 체크

### 6-1. 정적 체크 (`manage.py check`, DB 를 열지 않음)
| ID | 조건 | 수준 |
|---|---|---|
| `sqlite_ops.W001` | sqlite3 별칭의 `OPTIONS.transaction_mode` 가 `IMMEDIATE` 가 아님 | Warning (`--deploy` 일 때만) |
| `sqlite_ops.W002` | `init_command` 에 `journal_mode=WAL` 이 보이지 않음 | Warning (`--deploy`) |
| `sqlite_ops.W003` | VFS 별칭이 있고 `CONN_MAX_AGE != None`, WSGI 배포 | Warning |

뺀 것과 그 이유:
- "다중 프로세스인데 InMemory 레이어 사용": check 시점에는 프로세스 수를 알 수 없다. wireview W006 이 이미 이 경우를 다룬다.
- NATS 토큰 누락 체크: `connect_options` 로도 인증을 넣을 수 있어 오탐이 난다.
- `timeout` 미명시 경고: Python sqlite3 의 기본값이 이미 5초다.
- SQLite 버전 하한 검사: Django 가 이미 한다.

### 6-2. `sqlite_doctor` (명시 실행, 실제 연결)
- 별칭마다 실제 `journal_mode`, `busy_timeout`, `synchronous`, SQLite 버전, 파일 크기, WAL 크기를 본다.
- 마운트 종류를 본다. 네트워크 파일시스템(NFS·SMB)이면 경고한다. 판정할 수 없으면 `unknown` 으로 적는다.
- Litestream 설정 파일을 읽고, 그 안의 DB 경로가 `DATABASES` 와 맞는지 대조한다.
- 채널 레이어 백엔드와 그 의미론 요약을 출력한다(§8 표 참조).
- 출력은 사람용 텍스트와 `--json` 두 가지다. 종료 코드는 0(문제 없음), 1(경고), 2(오류)다.

## 7. 헬스

- `caught_up`: 원격 최대 TXID == 로컬 최대 TXID
- `backlog`: 로컬 TXID 가 원격보다 앞서 있고, 그 상태가 `SQLITE_OPS_BACKLOG_GRACE`(기본 60초)보다 오래 지속됨
- `unknown`: 원격 조회 실패, 메타를 읽을 수 없음, 부팅 때 `keep-local` 로 진행함
- **"마지막 업로드 시각"은 지연 지표로 쓰지 않는다.** 쓰기가 없는 정상 DB 도 업로드 시각은 오래되기 때문이다.
- **원격 조회 비용**: 요청마다 S3 를 부르지 않는다. 백그라운드 스레드나 캐시로 N초(기본 15초)마다 갱신한다. 정확한 방식은 구현 단계에서 정한다.
- 헬스는 "지금 복제가 따라오는가"만 말한다. "복구할 수 있는가"는 별도의 주기적 복원 검증(`sqlite_doctor --restore-test`, 2단계)으로 본다.
- D3 실측: 끊김 동안 Litestream 의 로그와 메트릭에 아무것도 보이지 않았다. 그래서 헬스는 Litestream 의 자기 보고에 기대지 않고, TXID 비교로 직접 계산한다.

## 8. 배포 프로필과 채널 레이어

마스터 결정(2026-10-07): **channels-nats 를 추천하고, channels_redis 는 호환 레이어로 소개한다.**

| 프로필 | 구성 | 채널 레이어 |
|---|---|---|
| `single-server` | 프로세스 1개(uvicorn/daphne 1 worker) + SQLite(WAL) + Litestream 사이드카 | `InMemoryChannelLayer` |
| `single-server-multiproc` | 프로세스 여럿 + 위와 같음 + nats-server 바이너리 | **channels-nats (추천)** / channels_redis (호환, Redis 가 이미 있을 때) |

- **레이어별 의미론 차이는 숨기지 않고 표로 문서화한다.** 추천을 바꿨다고 이 차이가 사라지지는 않는다.

  | | channels-nats | channels_redis |
  |---|---|---|
  | 수신자 생성 전에 보낸 메시지 | 사라짐(실측 0/5) | 보관됨(만료까지) |
  | 순서 보장 | 구독 하나 안에서만 | 채널 단위 |
  | `ChannelFull` | 발생하지 않음 | 발생함 |
  | 디스크 쓰기 / Litestream 영향 | 없음 | 없음(Redis 쪽) |
  | 1:1 지연 p50 (로컬 실측) | 0.3ms | (이번에는 재지 않음) |

- SQLite 기반 레이어(channels-lite)는 추천하지 않는다. 근거는 다음과 같다.
  - 폴링 간격과 유휴 CPU 를 맞바꿔야 한다. 0.1초 간격이면 p50 51ms·유휴 CPU 59ms/10초, 0.001초 간격이면 p50 2.3ms·유휴 CPU 1.9초/10초(코어의 약 19%)다.
  - 메시지마다 DB 쓰기가 2번 생긴다.
  - 만료 정리 DELETE 가 확률적으로 돈다.
- 그래도 channels-lite 를 쓰는 경우의 규칙(문서와 doctor 경고):
  - 채널용 DB 는 **별도 파일**로 둔다.
  - 그 파일은 Litestream 복제 대상에서 **뺀다**.
- channels-nats 문구 조정(README 위상 절, wireview 문서)은 OSS PO 마르코의 kanban `t_2cccec95` 에서 진행한다. 이 저장소에서는 하지 않는다.

## 9. channels-lite aio 결함 — 우리 쪽 몽키패치

마스터 결정(2026-10-07): **상류에 알리지 않는다. 필요하면 우리 라이브러리에서 몽키패치한다.**

- **결함**: `channels_lite.layers.aio.AIOSQLiteChannelLayer._receive_single_from_db` 는 `UPDATE ... SET delivered=1 WHERE id=? AND delivered=0` 다음에 `if conn.total_changes > 0:` 로 선점 성공을 판정한다(0.4.0, commit 72060cc, `aio.py:172`). `total_changes` 는 그 연결이 열린 뒤 누적된 변경 수다. 그래서 풀에서 재사용된 연결이면 UPDATE 가 0행이어도 참이 되고, 경쟁에서 진 수신자도 같은 메시지를 받는다.
- **검증 상태**: 코드상 확인. 재현은 아직 안 함.
- **구현 순서**(지키기):
  1. **먼저 재현 테스트를 만든다.** 수신자 둘이 같은 일반 채널(`!` 없는 채널)을 경쟁하고, 같은 연결이 앞서 쓰기를 한 상태를 만든다. 패치 전에 중복 배달이 실패로 드러나야 한다. 재현이 안 되면 패치하지 않고 이 절을 "재현 안 됨"으로 갱신한다.
  2. 패치는 `cursor.rowcount == 1` 로 판정을 바꾸는 최소 교체다. 메서드 하나만 바꾼다.
  3. **버전 게이트**: 설치된 channels-lite 버전이 검증한 범위(`==0.4.0`)일 때만 적용한다. 범위 밖이면 적용하지 않고 `sqlite_doctor` 에 경고로 남긴다.
  4. **명시 적용**: 자동으로 적용하지 않는다. 설정 `SQLITE_OPS = {"PATCH_CHANNELS_LITE_AIO": True}` 이거나 `django_sqlite_ops.compat.channels_lite.apply()` 를 호출할 때만 적용한다. `apply()` 를 두 번 불러도 안전해야 한다(멱등).
  5. 패치 후 같은 재현 테스트가 통과해야 한다.
- ORM 판(`layers/core.py`)은 `aupdate()` 의 반환값(행 수)을 검사하므로 해당하지 않는다.

## 10. 회귀 랩 (실패 경계)

스파이크 랩(`docs/reference/compose.yaml`, `Dockerfile`, `entrypoint.sh`)을 바탕으로 `lab/` 에 다시 만든다. S3 대체는 SeaweedFS 다(MinIO 는 익명 pull 이 막혀 있었다). 장애 주입은 toxiproxy 로 한다.

| ID | 시나리오 | 기대 |
|---|---|---|
| L1 | 볼륨 없이 새 컨테이너 | `fresh` → 복원, 행 수 일치 |
| L2 | 옛 볼륨으로 재부팅(D4) | `unknown` → 거부(exit 2), 복제본 손상 0 |
| L3 | 로컬 메타만 삭제 | `unknown` → 거부 (스파이크는 여기서 새 DB 를 버렸다) |
| L4 | S3 끊김 중 부팅 | `unknown` → 거부 |
| L5 | 복제되지 않은 로컬 커밋이 있는 상태로 재부팅 | `match` → 진행, 커밋 보존 |
| L6 | `--on-unknown restore` 진행 중 kill → 재시작 | 반쯤 옮겨진 상태 없음, 재실행으로 완료 |
| L7 | 복원 실패(S3 객체 손상) | exit 4, 로컬 무변경 |
| L8 | 헬스: S3 20초 끊김 | `backlog` → 복구 후 `caught_up` |

- 랩 자원은 compose 프로젝트명과 라벨로 구분하고, 끝나면 반드시 `down -v` 한다. 같은 호스트에 다른 프로젝트 컨테이너가 있다.
- ASGI(uvicorn) 엔트리포인트로 돌린다. 스파이크는 WSGI(gunicorn)만 검증했다.

## 11. 테스트·검증 원칙
- 판정 함수(`decide.py`)는 모든 입력 조합을 표 기반 단위 테스트로 고정한다.
- Litestream CLI 출력 파싱은 실제 0.5.17 출력을 fixture 로 저장해 테스트한다.
- 결함 수정은 "수정 전에 실패하는 테스트"로 증명한다.
- "재현함 / 코드상 확인 / 미검증"을 구분해서 쓴다.

## 12. 미검증·확인 필요
- ASGI 에서의 `CONN_MAX_AGE` 와 VFS 동작 (미검증)
- 실제 클라우드 S3·R2·Tigris (미검증. 비용이 들어 마스터 승인 필요)
- Windows 에서 boot CLI 의 파일 잠금과 디렉터리 rename 원자성 [확인 필요]
- channels-nats D2 의 196/200 이 하네스 문제인지 [확인 필요]
- "같은 이력" 증명 방법(자동 판정의 전제) [확인 필요]
- Litestream 상류 #1506(VFS 확장 로드), #1271·#1363(writable VFS) 머지 여부
