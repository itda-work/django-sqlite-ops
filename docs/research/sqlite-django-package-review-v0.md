# SQLite 전용 Django 패키지 설계 검토 v0 (2026-10-07, 아이스버그)

> 마스터 요청: dj-lite·channels-lite를 살펴보고, itda-work 조직에 "Django에서 수준 높은 SQLite를 쓰게 하는" 별도 패키지를 두어 django-wireview·channels-nats와 연계하는 안을 검토한다. 이 v0은 gpt-6-astra 교차 리뷰 전 초안이다.

## 결론 먼저
- **만들 가치는 있습니다.** 다만 이름을 바꿔야 합니다. "SQLite 설정 패키지"가 아니라 **"SQLite 운영 패키지"**입니다.
  - 설정 dict를 만들어 주는 부분은 dj-lite가 이미 잘하고, 116줄짜리라 차별점이 되지 않습니다.
  - 아무도 메우지 않은 칸은 운영입니다. Litestream 복제·복원, 오래된 로컬 DB 감지, 복제 지연 감시, 배포 점검(`check --deploy`), VFS 함정 우회. 오늘 Litestream 1·2부 실측에서 나온 위험 대부분이 이 칸에 있습니다.
- **채널 레이어는 직접 만들지 않는 쪽을 권합니다.**
  - 실측 결과 SQLite 폴링 레이어(channels-lite)는 프로세스 간 지연 p50이 약 52ms, 200명 fan-out이 135~176ms였습니다. channels-nats는 0.3ms와 1ms입니다.
  - wireview의 실시간 상호작용에는 NATS가 맞고, SQLite 레이어는 "브로커 0개, 트래픽 낮음" 구간에만 맞습니다.
  - 그래서 새 패키지는 레이어를 소유하지 않고, **배포 프로필과 점검**으로 셋(SQLite + channels-nats + wireview)을 묶는 것이 맞습니다.
- 착수, 이름, 범위는 아래 "마스터 결정 목록"에서 정해 주세요.

---

## 1. 두 프로젝트 살펴본 결과

### dj-lite (adamghill, MIT, ★81, v1.3.0, 마지막 커밋 2025-11-09)
- 정체: `sqlite_config(BASE_DIR)` 하나가 `DATABASES['default']` dict를 돌려줍니다(`src/dj_lite/configurator.py`, 116줄).
- 기본값: `transaction_mode=IMMEDIATE`, `timeout=5`, `init_command`로 `journal_mode=WAL`, `synchronous=NORMAL`, `temp_store=MEMORY`, `mmap_size=128MB`, `journal_size_limit≈26MB`, `cache_size=2000`.
- 좋은 점:
  - Django 5.1+의 공식 옵션(`transaction_mode`, `init_command`)만 씁니다. 백엔드를 갈아끼우지 않아 위험이 낮습니다.
  - 작성자의 "definitive guide"가 Litestream과 Docker 운영까지 다룹니다.
- 비어 있는 곳(코드 확인):
  - 실행 시점 검증이 없습니다. system check가 없어서, 예를 들어 NFS 위의 WAL, 오래된 SQLite 버전, 다른 연결이 바꾼 journal_mode를 잡지 못합니다.
  - 백업·복제·복원 기능이 없습니다(문서 링크만 있음).
  - 다중 DB(테넌트, 채널 전용 DB)를 보조하지 않고, 테스트 설정 프로필도 없습니다.
  - `typeguard` 런타임 의존이 하나 있습니다.
- 판정: **통과(범위가 좁음).** 경쟁하기보다 그 위에 올리거나 호환하는 편이 낫습니다.

### channels-lite (Tobi-De, ★1, v0.4.0, 마지막 커밋 2026-01-08, Alpha, 저장소 라이선스 표기 없음)
- 정체: Channels 레이어를 SQLite 테이블(`Event`, `GroupMembership`)로 구현했습니다. ORM 판(`SQLiteChannelLayer`)과 aiosqlite 판(`AIOSQLiteChannelLayer`, 연결 풀, msgpack)이 있습니다.
- 동작 방식: 0.1초 폴링입니다(`polling_interval`). 수신은 `SELECT` 후 `UPDATE delivered=1`로 원자적 선점을 합니다(`layers/core.py:44-49`). 만료 정리는 폴링 중 1% 확률로 돕니다.
- 실측(Django 6.1.2, Channels 4.3.2, Python 3.13.15, SQLite 3.53.4, 프로세스 간, macOS 로컬, 부하 수치 아님). 로그: `~/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/verify2.log`
  - **A. 1:1 지연(300건)**
    - channels-nats: p50 0.3ms / p99 0.5ms
    - lite ORM: 51.8ms / 103ms
    - lite aio: 53.3ms / 103ms
  - **B. 200명 그룹 × 20회 fan-out, 모두 도착할 때까지**
    - channels-nats: p50 1ms (4000/4000)
    - lite ORM: 171ms (4000/4000)
    - lite aio: 135ms (4000/4000)
  - **C. 유휴 10초, 대기 채널 200개**: 셋 다 WAL 증가 0, 다른 연결 커밋 0. 폴링은 읽기만 하므로 유휴 시 Litestream 복제 부담은 없습니다.
  - **D1. 수신자가 생기기 전에 보낸 일반 채널 메시지**
    - channels-nats: 0/5 (규약대로 사라짐, README에 명시됨)
    - lite: 5/5 (DB에 보관)
  - **D2. 일반 채널, 경쟁 수신자 2개, 버스트 200건**
    - channels-nats: 196/200 수신, 중복 0
    - lite: 용량 100에서 `ChannelFull` 75~81건(규약상 정상 backpressure), 중복 0, 한쪽 수신자에 몰림(119/0, 0/125)
- 발견한 결함·위험:
  - **aio 레이어는 `close()`를 부르지 않으면 프로세스가 끝나지 않습니다**(관찰: `asyncio.run`이 30초 넘게 반환하지 않았고, close를 넣자 즉시 끝남). 1차 벤치 TIMEOUT의 원인이었습니다. Channels 워커·관리 명령에서 그대로 걸릴 수 있습니다.
  - 오류를 `print`로 냅니다(`layers/__init__.py:327`). 잠금 판정도 문자열 `"locked" in str(e)`로 합니다(`:169`).
  - 메시지마다 INSERT 1번과 UPDATE 1번, 즉 **쓰기 2번**입니다. 앱 DB와 같은 파일에 두면 Litestream이 일시 데이터까지 복제합니다. 별도 DB 파일로 두고 복제에서 빼야 합니다.
  - 저장소에 라이선스 파일이 없습니다(README 배지는 MIT). 마지막 릴리스 뒤 9개월간 움직임이 없고, 메인테이너가 한 명입니다. 우리 제품이 의존하기에는 위험합니다.
- 판정: **위험(개념 검증 수준).** "브로커 없는 Channels"라는 방향은 맞습니다. 지연 50ms는 폴링 구조의 하한이라 튜닝으로 없어지지 않습니다(SQLite에는 LISTEN/NOTIFY가 없음).

---

## 2. 빈칸 지도: 누가 무엇을 메우나
- **연결 설정**(WAL, IMMEDIATE, pragma): dj-lite, Django 5.1+ 공식 옵션. 대체로 메워져 있습니다.
- **실행 중 검증**(설정이 실제로 적용됐나, 파일시스템, 버전): 아무도 없습니다.
- **백업·복제**: Litestream(외부 바이너리). Django와 이어 주는 것은 아무도 없습니다.
- **복원 안전성**(오래된 로컬 DB 덮어쓰기): 아무도 없습니다. 오늘 D4 `guard.py`가 첫 사례입니다.
- **복제 지연 관측**: 아무도 없습니다. Litestream 메트릭으로 드러나지 않습니다(D3 실측).
- **읽기 복제본**(follow/VFS 라우터, `CONN_MAX_AGE`, Linux auto-extension 우회): 아무도 없습니다.
- **멀티 프로세스 실시간**: channels-nats(우리), channels_redis, channels-lite(폴링)
- **캐시·작업 큐**: Django DB 캐시, Django 6.0 Tasks 프레임워크의 DB 백엔드. 상태는 [확인 필요]입니다.
- **테넌트별 DB 파일**: 아무도 없습니다(S4 실측에서 Django 동적 별칭이 숙제로 남음).

## 3. 제안 패키지 (가칭 `django-sqlite-ops`. 이름은 결정 사항)

### 3.1 범위(안)
1. **프로필 + system checks**: 등급은 Error/Warning이고, `check --deploy`에서만 뜨는 항목도 있습니다.
   - `transaction_mode != IMMEDIATE`, WAL이 아님, `timeout`/`busy_timeout`이 없음
   - SQLite 버전 하한, DB 경로가 네트워크 파일시스템
   - `CONN_MAX_AGE=0`인 VFS 별칭(1·2부 실측: 요청당 32~1,008ms)
   - 채널 레이어가 InMemory인데 프로세스가 여럿(wireview 체크와 역할 분담)
   - Litestream 설정이 있는데 복제 대상에 일시 데이터용 DB(채널, 세션 캐시)가 들어감
   - 설정 생성은 dj-lite와 **호환 API**(`sqlite_config(...)` 결과를 그대로 받음)로 합니다. 자체 생성기는 얇게 둡니다.
2. **Litestream 통합**(바이너리는 사용자가 설치. 패키지는 래핑만):
   - `manage.py sqlite_serve -- <gunicorn/uvicorn 명령>`: guard → restore → migrate → `exec litestream replicate -exec`. 2부 D1·D4의 엔트리포인트를 Python으로 옮긴 것입니다.
   - `manage.py sqlite_guard`: 로컬과 복제본 TXID를 비교해 격리·복원합니다. 메타 구조 의존은 버전 게이트로 감쌉니다.
   - 헬스 뷰와 메트릭: 복제 지연(버킷 최신 LTX 시각, 로컬 txid), 마지막 성공 업로드 시각. D3의 "조용한 실패"를 메웁니다.
   - 버전 매트릭스: 지원하는 Litestream 버전을 명시하고, CI에서 SeaweedFS + toxiproxy compose로 2부 시나리오를 회귀 테스트합니다.
3. **읽기 복제본 라우터**(선택 extra `[vfs]`): follow 파일과 VFS 두 방식. Linux auto-extension 우회(#1506 머지 전), `CONN_MAX_AGE` 강제 체크를 넣습니다.
4. **연계 프로필**(코드가 아니라 검증된 조합):
   - `single-server`: SQLite(WAL) + Litestream + `InMemoryChannelLayer` + 프로세스 1개
   - `single-server-multiproc`: SQLite + Litestream + **channels-nats** + uvicorn/daphne N개. wireview 배포 가이드의 현재 권장 그대로입니다.
   - `zero-broker`(선택, 실험): 별도 DB 파일의 SQLite 폴링 레이어. 트래픽이 낮고 지연 50~150ms를 받아들이는 경우만입니다.
5. **하지 않을 것(비목표)**:
   - DB 백엔드 교체(Turso/libSQL). 오늘 스파이크에서 `create_function` 공백 때문에 Django ORM 함수가 깨졌습니다.
   - 쓰기 가능 VFS. 상류 손상 버그가 열려 있습니다.
   - 다중 쓰기 노드, 리더 선출(LiteFS 영역)

### 3.2 django-wireview 연계
- 바꾸지 않는 것: wireview는 Channels 레이어 위에만 서 있습니다(`wireview/core/transport.py`의 `ChannelsBroker` → `group_send`/`group_add`). 레이어나 DB 백엔드를 알 필요가 없습니다.
- 연결점:
  1. 체크 연동: wireview의 `check_channel_layer`(`wireview/checks.py:502`)가 이미 "프로세스 여럿이면 channels_redis/channels-nats"를 경고합니다. 새 패키지는 SQLite 쪽 위험만 맡고 메시지는 중복시키지 않습니다.
  2. `wireview` 스타터 템플릿(`wireview/project_template`)에 선택 플래그로 이 패키지 프로필을 넣는 안. 결정이 필요합니다.
  3. 업로드 저장소(`features/upload_store.py`)는 파일시스템 기반이라 단일 서버 전제와 맞습니다. 다만 Litestream이 이를 백업하지 않는다는 점을 배포 문서에 명시해야 합니다.
  4. wireview의 `model_state`가 SQLite에서 피해 간 함정(`core/model_state.py:131`)처럼, ORM+SQLite 경계 사례를 이 패키지의 테스트 매트릭스로 공유할 수 있습니다.

### 3.3 channels-nats 연계
- channels-nats는 **기능 동결 상태**입니다(README "위상", 2026-09-28). 새 패키지가 channels-nats 코드 변경을 요구해서는 안 됩니다.
- 연결점:
  - 배포 프로필 `single-server-multiproc`의 표준 레이어로 지정합니다.
  - `check --deploy`로 nats URL·토큰 설정을 점검합니다.
  - nats-server 프로세스를 같은 `sqlite_serve` 감독 아래 둘지는 결정 사항입니다. 반대 의견도 있습니다: Windows 서비스 운영 방식과 충돌할 수 있습니다.
- D1 결과(수신자가 없을 때 보낸 메시지가 사라짐)는 SQLite 레이어와 의미론이 다릅니다. 프로필 문서에 "레이어를 바꾸면 달라지는 것"을 표로 남겨야 합니다.

## 4. 위험
- **포지셔닝**: "SQLite in production" 담론은 adamghill(dj-lite + 가이드)과 Rails 진영(Solid Queue/Cache/Cable)이 주도합니다. 경쟁 패키지로 보이면 얻는 게 적습니다. dj-lite 호환을 명시하고, 우리 차별점은 운영과 실측으로 둡니다.
- **Litestream 내부 의존**: 가드가 메타 디렉터리 구조와 `litestream ltx` 출력 형식에 기대고 있습니다. 버전이 오를 때마다 깨질 수 있어, CI 회귀 테스트가 없으면 유지가 어렵습니다.
- **유지보수 부담**: channels-nats는 동결, wireview는 1.x 안정입니다. 세 번째 공개 라이브러리는 마르코(OSS PO)의 관문과 릴리스 운영 부담을 늘립니다.
- **Django 본체 흐름**: Django가 SQLite 프로덕션 기본값을 본체에서 강화할 가능성이 있습니다(예: 5.1의 `transaction_mode`·`init_command`). 그러면 설정 부분은 사라질 수 있지만, 운영 부분은 남습니다. [확인 필요: 6.x 이후 관련 티켓]

## 5. 단계안(안)
- **0단계(1~2일, 스파이크)**: `sqlite_guard`, `sqlite_serve`, system checks 6개를 `django-lab`에서 시제품으로 만들고, 2부 Docker 랩으로 회귀 테스트합니다.
- **1단계(공개 0.1)**: checks + guard + serve + 헬스 뷰. 문서는 "검증된 조합 3개"입니다.
- **2단계**: 읽기 복제본 라우터 `[vfs]`, 테넌트별 DB 보조(S4 결과)
- **보류**: 자체 SQLite 채널 레이어. channels-lite의 성숙도를 지켜보거나 상류에 기여하는 쪽을 먼저 봅니다.

## 6. 마스터 결정 목록
1. 착수 여부: 0단계 스파이크까지만 할지, 공개 라이브러리로 갈지
2. 범위: 위 3.1의 1~4 중 0.1에 넣을 것. 권장은 1+2입니다.
3. 채널 레이어: (a) 만들지 않고 channels-nats 프로필 (b) channels-lite에 기여 (c) 자체 구현. 권장은 (a)입니다.
4. 이름: `django-sqlite-ops` / `django-litestream` / 기타
5. wireview 스타터 템플릿에 선택 프로필로 넣을지
6. 이슈 등록과 PO 배정: 마르코(OSS)에게 넘길지
