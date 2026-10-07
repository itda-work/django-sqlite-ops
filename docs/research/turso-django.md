# Turso란 무엇이고, Django에서 어떻게 쓸 수 있나 (2026-10-07, 아이스버그)

## 1. Turso란
"Turso"라는 이름이 세 가지를 가리켜서 헷갈립니다. 먼저 이 셋을 나눠 두어야 합니다.

- **Turso(회사)**: 처음 이름은 ChiselStrike였습니다. ScyllaDB 출신인 Glauber Costa와 Pekka Enberg가 세웠습니다.
- **libSQL**: SQLite C 코드를 포크한 것입니다(MIT, 2022~). Hrana 프로토콜을 써서 HTTP·WebSocket으로 원격 접속할 수 있고, 임베디드 레플리카와 암호화 기능이 있습니다. 지금 Turso Cloud가 이 위에서 돌아갑니다. 새 기능 개발은 멈췄고 유지보수만 합니다. 셀프호스팅용 `libsql-server`의 마지막 릴리스는 2025-02(v0.24.32)입니다.
- **Turso Database**: 예전 코드명은 Limbo입니다. Rust로 SQLite를 처음부터 다시 짠 것이고 포크가 아닙니다.
  - SQLite 파일 포맷·SQL과 호환되며, 기준 버전은 SQLite 3.50.4입니다.
  - SQLite에 없는 기능: MVCC로 동시 쓰기(`BEGIN CONCURRENT`), 자동 갱신되는 materialized view, FTS, 비동기 I/O(io_uring), 오프라인 양방향 sync.
  - 2026-07부터 같은 VM 코어 위에 Postgres 프런트엔드를 실험적으로 만들고 있습니다(pgmicro). 회사는 이를 "DB계의 LLVM"이라고 부릅니다.
  - 아직 1.0이 아닙니다. 저장소 태그는 v0.8.0-pre 계열이고 PyPI의 `pyturso`는 0.8.1입니다. README는 "여러 조직이 프로덕션에서 쓴다"면서도 "백업을 권장한다"고 적고 있습니다.
- **Turso Cloud**: SQLite를 관리형 서비스로 제공합니다. 2026-10 가격 페이지 기준으로 Free 플랜은 DB 100개, 5GB, 월 읽기 5억 행, 월 쓰기 1천만 행입니다. Developer 플랜은 월 $4.99, Scaler 플랜은 월 $24.92입니다. 쉬고 있는 DB는 저장 비용만 나가서, DB를 아주 많이 만드는 구조(DB-per-tenant)에 유리합니다.

출처:
- https://github.com/tursodatabase/turso
- https://turso.tech/blog/a-new-modern-version-of-postgres-in-rust
- https://www.theregister.com/databases/2026/07/29/after-rewriting-sqlite-in-rust-turso-turns-its-sights-on-postgres/5279835
- https://turso.tech/pricing
- https://pypi.org/project/pyturso/

## 2. 직접 돌려 본 결과 (Django 6.1.2 + Python 3.13 + pyturso 0.8.1, 로컬 파일 DB)
작업 위치: `~/Apps/django-lab/spikes/turso/` (`run.py`, `mvcc.py`, `run1.log`)

Django 내장 sqlite3 백엔드를 상속하고, DB-API 모듈만 `turso`로 바꿔 끼워 봤습니다.

### 그대로 끼우면 안 되는 이유 (공식 백엔드가 없어서 직접 메워야 하는 부분)
1. Connection에 `getlimit()`이 없습니다. Django는 이 값으로 파라미터 개수 상한을 계산하므로 모든 쿼리가 실패했습니다.
2. 커서 래퍼가 필요합니다. Django는 `%s` 자리표시자를 넘기는데, sqlite3 백엔드는 이를 자체 `SQLiteCursorWrapper`(`cursor(factory=...)`)에서 `?`로 바꿉니다. pyturso에서는 이 래퍼를 쓸 수 없어서 `near "%": syntax error`가 났습니다.
3. `isolation_level=None`을 적용해야 합니다. 이걸 빠뜨리면 `cannot start a transaction within a transaction` 오류로 migrate와 atomic이 깨졌습니다.
4. Connection에 `create_function`이 없습니다. 그래서 Django가 등록하는 `django_*` SQL 함수가 없고, `TruncDate`에서 `no such function: django_datetime_cast_date`가 났습니다. 같은 계열인 Trunc·Extract 일부, 시간대 변환, 일부 수학 함수도 영향을 받을 것으로 보입니다 [미검증].

### 위 1~3을 얇은 래퍼(약 30줄)로 메운 뒤 결과
- 통과: makemigrations, migrate(auth·contenttypes 포함), create, bulk_create, icontains, F() update, aggregate, datetime 읽기, `__year`, `__regex`, create_user, atomic 롤백
- 실패: TruncDate (위 4번이 원인)
- MVCC 동시 쓰기(`mvcc.py`): 스레드 4개가 각각 200건씩 `BEGIN CONCURRENT`로 넣었더니 800건이 모두 커밋됐고, 재시도는 0번, 걸린 시간은 0.04초였습니다. 로컬 파일 기준이며, SQLite의 단일 쓰기 잠금을 피하는 것은 확인했습니다.
- 돌리지 않은 것 [미검증]: Django test suite 전체, Turso Cloud 원격 모드(계정 가입이 필요해 하지 않았습니다), 서드파티 백엔드 패키지

## 3. Django 입장에서 의미

### 기회
- **"SQLite로 프로덕션" 흐름의 다음 단계**: Django 5.1부터 `init_command`와 `transaction_mode`로 SQLite를 프로덕션에서 쓰는 사례가 늘었습니다. 남은 약점은 두 가지였습니다.
  - 쓰기가 한 번에 하나뿐이다 → Turso의 MVCC가 겨냥하는 지점
  - 서버 여러 대가 같은 DB를 공유하기 어렵다 → Turso Cloud와 임베디드 레플리카가 겨냥하는 지점
- **DB-per-tenant**: 테넌트마다 SQLite 파일 하나를 주고, 쉬고 있으면 비용이 거의 0입니다. django-tenants(Postgres 스키마 방식)를 대신할 수 있는 구조입니다. 다만 Django `DATABASES`는 정적으로 정의하므로 동적 DB 라우팅을 직접 짜야 합니다.
- **로컬 우선·에지 배포**: 쓰기는 로컬 파일에 하고 클라우드와 sync합니다. 키오스크, 데스크톱 앱, 에이전트용 DB, 강의 실습(가입 없이 파일 하나로 시작)에 맞습니다.
- **Postgres 프런트엔드가 성숙하면**: Django의 postgresql 백엔드를 그대로 쓰는 경로가 생길 수 있습니다. 아직 실험 단계입니다 [확인 필요].

### 위험
- **공식 Django 백엔드가 없습니다.** 커뮤니티 패키지만 있습니다.
  - `django-libsql-backend` v0.1.3 (2026-07): 표준 라이브러리 HTTP로 원격 접속하는 방식이고 개인 프로젝트입니다.
  - `lincolnloop/django-libsql`, `aaronkazah/django-libsql`도 있습니다.
  - 셋 다 이번에 돌려 보지 않았습니다 [미검증].
- **원격(HTTP) 모드는 쿼리마다 네트워크를 탑니다.** ORM의 N+1 쿼리가 그대로 지연 시간으로 바뀝니다. 실익은 로컬 파일이나 임베디드 레플리카 모드에 있습니다.
- **엔진 자체가 1.0 전입니다.** 위의 4번 같은 sqlite3 모듈과의 API 차이가 남아 있습니다. 동시 쓰기를 쓰려면 Django 쪽에 `BEGIN CONCURRENT` 진입점과 커밋 충돌 시 재시도 처리가 필요한데, 둘 다 기본 제공되지 않습니다.
- **libSQL 기반 서드파티 백엔드는 이미 유지보수만 하는 줄기에 묶여 있습니다.** Rust 엔진으로 옮겨 가는 비용이 나중에 생길 수 있습니다.

## 4. 판정
- 지금 프로덕션 기본값: Postgres를 유지합니다. 단일 서버 소규모 서비스라면 SQLite(WAL) + Litestream 정도면 충분합니다.
- Turso를 써 볼 만한 곳: 테넌트별 DB, 로컬 우선 앱, 강의·데모용 "파일 하나 DB + 클라우드 sync".
- 우리 라이브러리와의 연결점 [아이디어]: pyturso 위에 얇은 Django 백엔드를 만들 수 있습니다. 위 1~3은 해결했고 남은 것은 4번과 `BEGIN CONCURRENT`입니다. 이건 스파이크 감으로, 착수 여부는 마스터가 결정합니다.
