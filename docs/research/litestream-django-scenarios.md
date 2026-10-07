# Litestream × Django 활용 시나리오 검증 (2026-10-07, 아이스버그)

- 환경: macOS arm64, Python 3.13, Django 6.1.2, SQLite 3.53.4(Python 내장), Litestream 0.5.17(GitHub 릴리스, SHA256 확인), litestream-vfs 0.5.17(PyPI)
- S3 대체: moto 5.2.3 로컬 서버. 실제 S3·R2·Tigris는 미검증입니다. 네트워크 지연이 붙으면 아래 지연 수치는 달라집니다.
- 코드: `~/Apps/django-lab/spikes/litestream/` (`s1_dr.py` … `s8_ops.py`). 결과 로그: `work/results.log`
- Django 설정: Django 5.1+ 권장값을 썼습니다(`transaction_mode=IMMEDIATE`, `init_command`로 WAL·`synchronous=NORMAL`·`busy_timeout`).

## 한눈 판정
- **S1 재해 복구(사이드카)**: 통과. 500건 중 500건 복원, integrity ok, 복원 0.11초, migrate 상태도 그대로였습니다.
- **S2 컨테이너 엔트리포인트(restore → migrate → replicate -exec gunicorn)**: 통과. 다만 함정이 둘 있습니다.
- **S3 시점 복원(PITR)**: 통과. 전체 삭제 사고 뒤 `-timestamp`로 사고 직전 300건을 살렸습니다.
- **RPO(전원 차단)**: 측정만 했습니다. 3회 평균 약 1초 분량의 쓰기를 잃었습니다(64·78·81건).
- **S4 테넌트별 DB 파일 + 디렉터리 복제**: 통과. 실행 중에 추가한 테넌트도 자동으로 복제됐고, 테넌트 하나만 복원할 수 있었습니다.
- **S5 읽기 복제본(`restore -f` + Django DB 라우터)**: 통과. 지연 약 1초, 읽기 1.5ms/요청이었습니다.
- **S6 VFS 읽기 복제본(S3에서 바로 읽기)**: 조건부 통과. `CONN_MAX_AGE`를 반드시 설정해야 합니다.
- **S7 Writable VFS를 Django 기본 DB로 쓰기**: 틀림. 지속적으로 쓰면 `database disk image is malformed`가 났습니다(상류 버그, 이슈 열림).
- **S8 마이그레이션·VACUUM 중 복제**: 통과. 다만 무중단 배포 순서 문제는 Django 쪽에 남습니다.

---

## S1. 재해 복구: 사이드카로 붙이기만 하면 됨
- 방법: `migrate` → `litestream replicate`(설정 파일) → ORM으로 500건 쓰기 → 종료 → 로컬 `app.sqlite3*` 전부 삭제 → `litestream restore -o`
- 결과: 500/500 복원, `integrity_check=ok`, 복원 0.11초, `showmigrations` 미적용 0, `makemigrations --check` "No changes"
- 관찰 1: Litestream이 원본 DB에 `_litestream_lock`, `_litestream_seq` 테이블을 만듭니다. Django 모델이 아니어서 `makemigrations`·`check`에는 영향이 없었습니다. 다만 `inspectdb`나 DB 비교 도구에는 보입니다.
- 관찰 2: Django 설정에 WAL을 넣지 않아도(S1b) Litestream이 시작하면서 `journal_mode`를 `delete`에서 `wal`로 바꿨습니다. 그래도 Django 쪽에서 WAL·`busy_timeout`을 명시하는 것을 권합니다. Litestream이 없는 개발·테스트 환경과 동작을 맞추기 위해서입니다.

## S2. 컨테이너 엔트리포인트 패턴 (Fly·Render·VM 공통)
```bash
litestream restore -if-db-not-exists -if-replica-exists -config ls.yml $DB_PATH
python manage.py migrate --noinput
exec litestream replicate -config ls.yml -exec "gunicorn config.wsgi -w 4 ..."
```
- 부하: gunicorn 워커 4개, 클라이언트 스레드 8개로 1,200건을 썼습니다. 에러 0, 초당 1,511건, p50 3.2ms, p99 25.8ms. `IMMEDIATE`와 `busy_timeout` 덕에 `database is locked`가 0건이었습니다.
- 정상 종료: litestream PID에만 SIGTERM을 보내도 gunicorn까지 내려가고, 마지막 쓰기까지 복제됐습니다(400/400). 컨테이너 PID1로 litestream을 두는 구성이 성립합니다.
- 새 머신: 빈 디렉터리에서 같은 엔트리포인트를 돌리면 자동 복원 후 손실 0(1,300건)으로 이어서 서비스했습니다.

### 함정 A: 부팅 직후 곧바로 종료하면 복제본이 비어 있을 수 있음
- 50건을 쓰고 즉시 SIGTERM을 보내자 복제본에 아무것도 없었습니다(`no matching backup files available`). 3초 기다린 뒤 종료하면 50/50이었습니다.
- 의미: 첫 스냅샷이 올라가기 전에 머신이 죽으면 그 데이터는 복제본에 없습니다. 배포 직후 헬스체크가 실패해 바로 롤백되는 경우가 여기에 해당합니다.

### 함정 B: 오래된 로컬 DB가 남은 머신이 다시 뜨면 복제본 최신본을 덮어씀 (가장 위험)
- 재현:
  1. 머신 A가 50건을 쓰고 종료합니다.
  2. 머신 B가 복원해 150건까지 씁니다.
  3. 디스크(볼륨)가 남아 있던 A가 재부팅합니다.
  4. A에서는 `-if-db-not-exists` 때문에 복원을 건너뛰고, 옛 50건으로 서비스를 시작합니다.
  5. A가 쓴 내용이 새 세대로 복제본에 올라갑니다.
- 결과: 최신 restore가 **55건**이 됐습니다. `-txid 2`로는 150건을 되살릴 수 있어서 데이터가 사라지지는 않았습니다. 하지만 기본 restore로는 옛 데이터가 나옵니다.
- 같은 버킷 경로에 두 머신이 동시에 replicate할 때(S2b)도 경고나 에러 로그 없이 마지막 쓴 쪽이 이겼습니다(160건이 남음).
- Django 운영 규칙:
  - 쓰는 머신은 반드시 1대입니다. Fly라면 볼륨 1개와 머신 1대, `max_machines_running=1` 수준입니다.
  - 볼륨이 남는 플랫폼에서는 부팅할 때 "로컬 DB의 TXID가 복제본보다 낮으면 복원을 강제"하는 확인을 넣거나, 볼륨 없이 매번 복원합니다.
  - 블루/그린 배포처럼 새 머신을 먼저 띄우는 방식에서는 옛 머신과 겹치는 시간 동안 두 쪽이 모두 씁니다. 이 경우 롤링 대신 정지 후 기동(stop-then-start)으로 배포합니다.

## S3. 시점 복원(PITR): "실수로 지웠어요" 대응
- 300건을 쓰고, 시각 T를 기록하고, `Note.objects.all().delete()`를 실행했습니다.
- `litestream restore -o pitr.sqlite3 -timestamp T` → 300건, 최신 restore → 0건.
- 주의: 기본 `l0-retention`이 5분이고 L1/L2/L3 압축 주기가 30초/5분/1시간이라, 오래될수록 복원 지점이 거칠어집니다. 세밀하게 되돌려야 하면 `l0-retention`과 `snapshot.retention`을 늘립니다(저장 비용이 늘어남).
- `restore -dry-run`은 replica URL만 줘도 `-o`가 필요했습니다. 문서 예시와 다르니 [확인 필요]입니다.

## RPO: 비동기 복제의 대가
- `sync-interval: 1s`에서 쓰기 도중 앱과 litestream을 동시에 SIGKILL(전원 차단 가정)한 뒤 복원했습니다.
- 3회 손실은 64·78·81건이고, 쓰기 속도로 환산하면 약 0.8~1.1초 분량입니다.
- 결론: "커밋 응답 = 내구성 보장"이 아닙니다. 결제처럼 1초 손실도 안 되는 데이터는 Postgres(동기 복제)로 둡니다. Litestream은 "1초 안팎 손실을 감수하는 백업"으로 설명해야 정확합니다.

## S4. 테넌트별 SQLite 파일 (B2B SaaS 패턴)
- 설정: `dbs: - dir: tenants/, pattern: "*.sqlite3", watch: true, meta-dir: ...`
- Django 쪽: 런타임에 `settings.DATABASES`와 `connections.settings`에 테넌트 별칭을 추가하고 `migrate(database=alias)`를 실행했습니다. acme 100, globex 200, initech 300건.
- 실행 중에 umbrella 테넌트를 추가하자 litestream 재시작 없이 복제본에 생겼습니다.
- `restore s3://…/tenants/globex.sqlite3`로 globex 하나만 복원했습니다(200건, integrity ok).
- 남는 과제(Django 쪽): 동적 DB 별칭은 공식 API가 아니라 `connections.settings`를 직접 건드려야 합니다. 테넌트 수만큼 migrate를 반복해야 하고, 연결 수도 관리해야 합니다. 라이브러리 감으로 볼 만한 지점입니다.

## S5. 읽기 복제본 A: `restore -f`(follow) + DB 라우터
- `litestream restore -f -o follower.sqlite3 <url>`로 로컬 파일이 복제본을 계속 따라가게 했습니다.
- Django: `DATABASES['replica'] = {'NAME': 'file:…/follower.sqlite3?mode=ro'}`와 라우터(읽기는 replica, 쓰기는 default, migrate는 default만).
- 결과: 지연 0.94~1.25초(5회), 읽기 1.5ms/요청(HTTP 포함), 쓰기는 default로 정상 라우팅.
- 쓸 곳: 읽기 전용 리포트·관리자 화면을 다른 머신이나 리전에 둘 때. 사이트 전체 읽기에 쓰면 "방금 쓴 글이 안 보이는" 1초 지연을 UX로 처리해야 합니다.

## S6. 읽기 복제본 B: VFS로 S3에서 바로 읽기
- 방법: 프로세스 시작 때 `litestream_vfs`를 한 번 `load_extension`하고 `DATABASES['replica']['NAME'] = 'file:replica.db?vfs=litestream'`로 둡니다. `LITESTREAM_REPLICA_URL`은 프로세스 밖(셸·프로세스 매니저)에서 줍니다. 문서상 `os.environ`으로 넣으면 보장되지 않는다고 합니다.
- 결과:
  - 지연 0.6~1.45초
  - VFS 연결로 ORM 쓰기를 시도하면 `OperationalError: attempt to write a readonly database`로 막힙니다. 안전합니다.
  - **핵심 함정**: Django 기본값 `CONN_MAX_AGE=0`이면 요청마다 VFS 연결을 새로 열면서 LTX 인덱스를 다시 만듭니다.
    - `CONN_MAX_AGE=0`: 32~48ms/요청 (뷰 안에서 잰 쿼리 시간 p50 31ms)
    - `CONN_MAX_AGE=None`: 2.0ms/요청 (쿼리 p50 0.26ms)
    - 원시 sqlite3 기준: 새 연결 60ms, 같은 연결 0.01ms
- 판정: Django에서 VFS 복제본을 쓸 거면 replica 별칭에 `CONN_MAX_AGE=None`(또는 충분히 큰 값)을 반드시 줍니다. 로컬 디스크가 없는 서버리스나 짧은 수명 워커에서 "S3에 있는 DB를 그냥 읽는" 용도로 성립합니다. 실제 S3에서는 첫 페이지마다 네트워크 왕복이 붙으니 리전을 맞춰야 합니다.

## S7. Writable VFS를 Django 기본 DB로: 틀림 (현재 쓰면 안 됨)
- 설정: `NAME='file:app.db?vfs=litestream'`, `LITESTREAM_WRITE_ENABLED=true`, `SYNC_INTERVAL=1s`, `CONN_MAX_AGE=None`.
- 된 것: migrate, 200건 쓰기(9.4ms/건), atomic 롤백, 새 프로세스에서 데이터 보임, `litestream restore` 결과 integrity ok.
- 안 된 것: 10ms 간격으로 계속 쓰자 몇 건 만에 `database disk image is malformed`가 났습니다. 원시 sqlite3로 재현한 결과는 다음과 같습니다.
  - 같은 연결로 sync 경계를 넘김(10ms, 100ms 간격, sync 100ms): 6개 조건 중 4개가 실패. 3~26건째에서 실패.
  - sync 전에 몰아 쓰기(0ms 간격 400건): 통과
  - 매 쓰기마다 다시 연결: 통과(300건)
  - 실패 뒤에도 S3 복제본은 integrity ok였습니다(실패 직전까지만 들어 있음). 깨지는 것은 쓰는 연결이 보는 화면 쪽입니다.
- 상류 이슈(모두 열림, 미해결):
  - benbjohnson/litestream #1271 "Writable VFS: FileSize race after sync flush causes 'database disk image is malformed'"
  - #1293, #1363 (지속 쓰기 중 손상 수정 PR)
  - #1018 (쓰기 버퍼 경쟁)
- 판정: 0.5.17 기준으로 Django 기본 DB를 writable VFS로 두는 것은 쓰면 안 됩니다. 상류 PR이 머지되면 다시 검증하겠습니다. "매 쓰기마다 다시 연결"로 피할 수는 있지만, 6번에서 본 연결 비용(60ms)이 붙어 실익이 없습니다.

## S8. 운영 작업과의 공존
- 5만 행(18.9MB)을 넣고 litestream을 돌리는 중에 필드를 추가하는 migration(SQLite는 테이블을 다시 만듦)과 VACUUM을 실행했고, 동시에 20ms 간격 쓰기 300건을 흘렸습니다.
- 복제 결과: 원본 50,294건과 복원본 50,294건이 일치, integrity ok, 새 컬럼 존재, `showmigrations` 둘 다 [X], litestream ERROR/WARN 0.
- Django 쪽 문제: 동시에 돌던 옛 코드 프로세스가 `table notes_note has no column named tag`로 6건 실패했습니다. 옛 모델에는 tag가 없어서 INSERT에 빠지는데, 새 컬럼은 DB 기본값 없이 NOT NULL로 만들어지기 때문으로 보입니다.
  - Litestream 문제가 아니라 "migrate와 구버전 코드가 겹치는 시간" 문제입니다.
  - 대응: Django 5.0+의 `db_default`를 쓰거나, 정지 후 기동 배포로 겹치는 시간을 없앱니다. 서버 한 대 SQLite 구성이면 S2 엔트리포인트 순서(migrate 후 앱 기동) 자체가 정지 후 기동입니다.

---

## 활용 시나리오 판정 (Django 관점)
1. **서버 한 대 Django + SQLite + Litestream 백업**: 권장합니다. S1·S2·S3·S8이 통과했습니다. 개인 서비스, 사내 도구, 강의 실습 서버, MVP에 맞습니다. 조건은 쓰는 머신 1대와 약 1초 RPO 감수입니다.
2. **컨테이너 플랫폼(Fly 등) 무상태 배포**: 권장합니다. 다만 함정 A·B 대응(쓰는 머신 1대, 정지 후 기동 배포, 볼륨 재사용 시 TXID 확인)을 런북에 넣어야 합니다.
3. **테넌트별 DB 파일**: 가능합니다. Litestream 쪽은 통과했고, Django 동적 DB 별칭 관리가 숙제입니다.
4. **읽기 복제본(follow 파일)**: 가능합니다. 리포트·관리자 화면처럼 약 1초 지연을 견디는 읽기에 씁니다.
5. **읽기 복제본(VFS)**: 조건부입니다. `CONN_MAX_AGE=None`이 필수이고, 실제 S3 지연은 미검증입니다.
6. **Writable VFS를 기본 DB로**: 지금은 금지입니다. 상류 손상 버그가 열려 있습니다.
7. **Postgres를 대신하는 범용 선택지**: 아닙니다. 다중 쓰기, 1초 미만 RPO, 수평 확장이 필요하면 Postgres입니다.

## Turso와의 비교 (앞 조사와 연결)
- **Litestream**:
  - 일반 SQLite 그대로입니다. Django 내장 백엔드를 수정 없이 쓰고, 이번 검증에서 ORM 쪽 문제는 0이었습니다.
  - 쓰는 쪽은 1대이고, 복제는 비동기(약 1초)입니다.
- **Turso**:
  - 엔진을 교체해 동시 쓰기와 동기화를 얻습니다.
  - 대신 Django 백엔드 공백이 있습니다(앞 스파이크에서 `create_function` 부재로 `TruncDate` 실패).
- 지금 Django 실무에 바로 넣을 수 있는 것은 Litestream입니다.

## 미검증 / 남은 일
- 실제 S3·R2·Tigris(네트워크 지연, 비용): 계정과 비용이 들어서 하지 않았습니다.
- Linux 컨테이너(Docker)에서의 엔트리포인트 동작: compose 단위 실행 판정은 징베 몫입니다.
- 장시간(수 시간 이상) 압축·보존 동작, 수 GB DB 복원 시간
- 함정 B를 막는 부팅 스크립트(로컬 TXID와 복제본 비교) 실물
- 상류 #1271/#1363이 머지되면 S7 재검증
