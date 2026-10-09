# django-sqlite-ops 설계서 v1

- 상태: 설계 확정 전 단계(v1). 구현은 아직 없다.
- 작성: Django전문가-아이스버그(itda-django), 2026-10-07
- 근거: `docs/research/` 의 보고서 6편과 교차 리뷰 원문. 수치는 모두 그 보고서의 실측이다.
- 이름 `django-sqlite-ops` 는 가칭이다. PyPI 배포 전에 바꿀 수 있다(→ `docs/DECISIONS.md` D-6).

---

## 1. 한 줄 정의

Django 프로젝트가 **SQLite 를 운영 DB 로 안전하게 쓰게 하는 운영 도구 패키지**다. 권장 설정, 부팅 순서, 복제 상태 확인, 진단, 배포 프로필을 맡는다. 권장 설정은 외부 라이브러리(dj-lite 등)에 기대지 않고 이 패키지가 직접 정의하고 검증한다(D-10).

## 2. 왜 운영인가 (설정은 그 출발점)

- **설정을 넣는 통로는 Django 가 이미 지원한다.** Django 5.1 부터 `OPTIONS` 에 `init_command`(PRAGMA)와 `transaction_mode` 가 들어왔다. 그러나 "어떤 값이 맞는가"는 운영 지식이고, 진단·체크가 비교할 기준이 된다. 그래서 권장값의 정본을 이 패키지가 갖는다(§6-0). dj-lite 에는 의존하지 않는다(D-10). 생성 함수는 표준 `DATABASES` 항목(dict)을 돌려줄 뿐이고, DB 백엔드를 교체하거나 감싸지 않는다.
- **비어 있는 칸은 운영이다.** Litestream 실측(`docs/research/litestream-django-*.md`)에서 드러난 사고는 모두 운영 경계에서 났다.
  - 옛 디스크로 재부팅하면 복제본이 **경고 없이** 덮어써진다(150행 → 55행).
  - S3 가 20초 끊긴 동안 WARN/ERROR 로그가 0줄이었다. `litestream status` 는 rc 0 을 돌려준다.
  - 두 머신이 동시에 replicate 하면 나중에 쓴 쪽이 경고 없이 이긴다.
  - VFS 읽기 복제본은 `CONN_MAX_AGE=0` 이면 요청마다 1,008ms 가 걸리고, `None` 이면 1.7ms 다(WSGI 기준).
  - Linux 에서 VFS 확장을 로드하면 그 뒤의 일반 `connect` 가 `automatic extension loading failed` 로 실패한다.

## 3. 범위

### 3-1. v0.1 에 넣는 것
0. **권장 설정**: 프로필별 권장값 표와 표준 `DATABASES` 항목을 돌려주는 생성 함수. 체크와 doctor 는 이 표를 기준으로 비교한다. (§6-0)
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
- DB 백엔드 교체·래핑. 설정은 표준 `DATABASES` dict 로만 낸다.
- dj-lite 의존·호환 API. 사용자가 dj-lite 결과를 넣어도 체크와 doctor 는 같은 기준으로 검사할 뿐, 그 형식을 따로 지원하지 않는다.
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
    [--adopt-existing]                         # 기존 DB 최초 도입 전용 (D-11)
    [--init-new]                               # 생애 첫 배포 전용: 빈 복제본에서 새 DB (D-13)
    [--meta-path PATH]                         # 기본 <db 디렉터리>/.<db 이름>-litestream
    [--litestream BIN] [--ltx-timeout S] [--restore-timeout S]   # 기본 litestream, 30, 600
    -- <다음 명령...>                          # 예: litestream replicate -exec "uvicorn ..."
```
구현은 `boot/cli.py`(진입점 `boot/__main__.py`)다. 표준 라이브러리만 쓴다. 단계마다 stderr 에 `[boot] ...` 한 줄을 남긴다(입력, 판정의 state·action·reason_code·reason, 조치, 결과). 거부하면 사유 코드별로 다음에 할 일을 `[boot] hint: ...` 한 줄로 안내한다.

**경로 계약: 실제 경로만 받는다**(D-15, `real_path()`). `--db`·`--meta-path` 는 다음을 모두 만족해야 하고, 아니면 exit 64 와 함께 실제 경로를 쓰라고 안내한다.
- 원문에 `..` 구성요소가 없다.
- `normpath(abspath(원문))` 의 마지막 구성요소가 `.`·`..`·빈 문자열이 아니다(파일 이름으로 끝난다).
- 부모가 존재하는 디렉터리이고 `realpath(부모, strict=True)` 가 부모와 같다. 즉 부모 경로 어디에도 심볼릭 링크·없는 구성요소가 없다. 마지막 구성요소는 없어도 된다(새 DB).

이렇게 정한 경로 하나를 잠금·사이드카·임시 복원·격리·manifest·설치·상태 파일, **그리고 Litestream 호출(`ltx`·`restore` 의 DB 인자)** 에 모두 쓴다. 그래서 Litestream 설정의 `dbs[].path` 도 같은 실제 경로여야 한다.
- 왜 좁혔나: 앞선 구현은 경로를 '고쳐' 썼다. `abspath` 는 `alias/..` 를 문자열로 접어 링크 뒤의 실제 위치와 다른 별개 DB 를 격리했고, 부모만 `realpath` 로 바꾸면 Litestream 이 설정의 `alias/app.db` 와 다른 DB 로 보아 `database not found in config` 가 났으며, `strict=False` 의 `realpath` 는 없는 구성요소 뒤의 `..` 를 접어(`missing/../app.db`) 입력이 가리키지 않는 기존 DB 를 옮겼다(모두 리뷰 재현). 고쳐 쓰지 않고 검증만 한다. 부모 링크 배포 지원은 필요해지면 다시 연다(D-15).
- 마지막 구성요소의 링크는 따로 다룬다: DB 는 정규 파일만 받고, 격리 대상(DB·사이드카·메타)은 링크면 격리를 거부한다. 판정·`PROCEED` 에서 메타 디렉터리 링크를 따라가는 조회 규약은 그대로다.
- 단계 함수(`quarantine`·`check_quarantinable`·`resume_quarantine`)를 직접 불러도 같은 계약을 검사한다.

1. **잠금 획득**: `<db>.boot.lock` 에 `flock(LOCK_EX | LOCK_NB)`. 이미 잡혀 있으면 exit 5. 잠금 fd 는 **exec 된 명령에 상속된다**. 그래서 `litestream replicate -exec ...` 와 그 자식이 살아 있는 동안 같은 볼륨에서 두 번째 boot 는 잠금에서 막힌다(exit 5). 거부·실패로 돌아올 때는 잠금을 풀고 끝난다. 잠금 파일이 심볼릭 링크거나 정규 파일이 아니면 exit 5, 잠근 뒤 경로의 inode 가 잠근 파일과 다르면(그 사이 바뀜) exit 5. **flock 의 한계**: 잠금은 파일(inode)에 걸리므로 운영자가 잠금 파일을 지우거나 바꾸면 다음 boot 는 새 파일을 잠가 보호가 사라진다. 획득 때의 검사는 그 순간만 보며 이후의 unlink 를 막지 못한다. 잠금 파일을 지우지 않는다. 다른 머신 사이의 이중 replicate 는 막지 못한다. 이는 문서와 `sqlite_doctor` 경고로 다룬다. `fcntl` 이 없는 플랫폼(Windows)은 판정할 수 없으므로 exit 64 다(§12).
2. **중단된 격리 재개**: `<db>.stale-<ts>.partial/` 이 남아 있으면 앞선 부팅이 격리 도중 죽은 것이다. 판정 전에 그 격리를 끝낸다(멱등, §4-3 격리). 둘 이상이면 판정할 수 없으므로 exit 2.
3. **판정 입력 수집**: 로컬 DB 존재(DB 경로가 정규 파일. 디렉터리·심볼릭 링크 등 그 밖의 것이면 exit 2), `local_max_txid()`, `remote_max_txid()`.
   - **DB 파일 없이 `-wal`/`-shm`/`-journal` 만 남은 경우**는 `decide()` 를 부르지 않고 `unknown/orphan_sidecars` 로 거부한다(exit 2). 남은 WAL 이 어느 DB 의 것인지 알 수 없고, 그 옆에 복원하면 옛 WAL 이 새 DB 에 섞인다. 단 `--on-unknown restore` 이고 원격에 복제본(TXID)이 있으면 이 파일들을 격리 대상에 넣고 복원한다(`quarantine_and_restore`).
4. **판정** (§4-3): `decide()` 한 번.
5. **조치**: 판정 결과에 따라 복원하거나, 그대로 두거나, 거부한다(아래 '조치 실행').
6. **무결성 확인**: DB 파일이 있으면 표준 `sqlite3` 로 읽기 전용 URI(`file:...?mode=ro`)를 열어 `PRAGMA quick_check` 가 `ok` 인지 본다. 아니면(열 수 없음 포함) exit 3. DB 파일이 없으면(새 DB) 건너뛴다. 확인하려고 빈 DB 를 만들지 않는다.
7. **상태 파일**: `<db>.boot-state.json` 을 원자적으로 쓴다(§7).
8. **exec**: `os.execvp` 로 `--` 뒤의 명령으로 프로세스를 교체한다. 실행하지 못하면(명령 없음 등) exit 127. `migrate` 는 그 명령 안에서(또는 운영자가 따로) 실행한다.

**조치 실행** (순서가 안전성이다)
- `PROCEED`: 아무것도 바꾸지 않는다. `fresh/new_db` 여도 DB 파일을 만들지 않는다(앱이 만든다, §4-1). `adopt` 나 `new_db` 로 진행하면 "첫 배포/도입 뒤에는 이 옵션을 끈다(D-11/D-13)" 한 줄을 남긴다.
- `RESTORE`·`QUARANTINE_AND_RESTORE`: **먼저** DB 와 같은 디렉터리(같은 파일시스템이어야 rename 이 원자적이다)에 매번 새 `<db>.restore-<ts>-<rand>/` 를 만들고 그 안으로 `restore()` 한다. 실패하면 로컬은 **하나도 바꾸지 않고** exit 4 다(L7). 실패한 임시 디렉터리는 조사용으로 남기고, 다음 부팅은 이를 판정에 쓰지 않는다. 복원 디렉터리에 DB 와 `-shm` 말고 다른 파일(`-wal` 등)이 있으면 DB 만 옮기면 내용이 빠지므로 실패로 본다. 복원이 성공했을 때만 격리 대상 중 **있는 것**(DB·사이드카·메타)을 격리한다. `RESTORE` 는 로컬 DB 가 없을 때이므로 보통 옮길 것이 없지만, TXID 를 읽을 수 없는 메타 디렉터리가 남아 있으면 그것을 옮긴다. 그다음 임시 DB 를 `os.rename` 으로 DB 경로에 놓는다(설치). 그 순간 DB 경로나 사이드카 자리에 무엇이 있으면 exit 2. 임시 디렉터리는 비운 뒤 지운다.
- `KEEP_LOCAL`: 바꾸지 않는다. stderr 에 `WARNING: unknown_at_boot` 한 줄, 상태 파일에 `unknown_at_boot: true`.
- `REFUSE`: exit 2. 사유와 사유 코드별 안내 한 줄.
- 격리·설치 도중의 파일 오류는 exit 4 다. 격리 도중 실패해 `.partial` 이 남으면 다음 부팅이 재개한다. 격리 완료·설치 이후 실패는 남은 상태에 따라 다음 부팅이 fresh 복원(로컬 DB·메타 없음) 또는 D-14 복구(DB 있음·메타 없음 → `no_local_meta`)를 따른다.

### 4-3. 판정 (상태 4가지)
| 상태 | 조건 | 기본 동작 |
|---|---|---|
| `fresh` | 로컬 DB 파일과 로컬 메타가 모두 없고, 원격 조회 성공. 원격이 빈 목록이면 `--init-new` 도 있어야 한다(D-13) | 원격에 복제본이 있으면 복원, 빈 목록(+`--init-new`)이면 그대로 진행(새 DB) |
| `match` | 로컬 메타의 최대 TXID ≥ 원격 최대 TXID, 그리고 원격 조회 성공 | 그대로 진행 |
| `adopt` | `--adopt-existing` 이 있고, 로컬 DB 있음 + 로컬 메타 없음 + 원격 조회 성공·빈 목록 (D-11) | 그대로 진행(기존 DB 를 처음 Litestream 에 올림) |
| `unknown` | 그 밖의 모든 경우: 원격 조회 실패·타임아웃·파싱 실패, 로컬 메타 없음, DB 없이 메타만 남음, 로컬 DB 는 있는데 원격이 빈 목록, 로컬 DB 도 원격도 없는데 `--init-new` 없음, 원격이 앞섬 | **기동 거부(exit 2)**. 사유를 한 줄로 출력 |

- `--on-unknown restore` : 복원을 임시 디렉터리에 먼저 끝낸 뒤, 로컬을 `<db>.stale-<ts>/` 디렉터리 하나로 옮기고 복원본을 설치한다. 여러 파일을 한 번에 옮기는 원자적 연산은 없으므로 **재개 가능하게** 옮긴다(L6).
  0. 복원 **전에** 격리할 수 있는지 본다(`check_quarantinable()`). 하나라도 어긋나면 아무것도 바꾸지 않고 exit 2: 대상 경로(DB·사이드카·메타) 자체가 심볼릭 링크, 대상 이름끼리 겹침, 대상 이름이 격리 디렉터리의 예약 이름(`manifest.json`, `manifest.json.tmp-*`)과 겹침, 다른 파일시스템.
     - **상하위 관계**: 격리 대상끼리 같은 경로이거나 한쪽이 다른 쪽의 조상이면, 또는 대상이 격리 목적지(DB 디렉터리 안의 `.partial`·final·임시 복원 디렉터리)를 포함하면 거부한다. 예: 메타가 DB 의 부모면 DB 를 옮긴 뒤 메타를 자기 안으로 옮기려다 실패하고 재개도 같은 오류를 반복한다(리뷰 재현). 재개 검증도 같은 검사를 한다.
     - **링크 제약(v0.1)**: 링크를 rename 하면 실체는 밖에 남고 격리본에는 끊어진 링크만 남는다(리뷰 재현). 링크 실체 보존은 지원하지 않으므로 격리가 필요한 조치(`RESTORE`·`QUARANTINE_AND_RESTORE`)에서는 거부한다. 판정·`PROCEED`·`KEEP_LOCAL` 에서는 아래 로컬 TXID 조회 규약대로 메타 디렉터리 링크를 따라간다(이 차이는 의도다).
  1. `<db>.stale-<ts>.partial/` 을 만들고, 그 안에 `manifest.json` 을 임시 파일 + rename 으로 쓴다. 스키마: `version`(1), `db`(대상 DB 절대 경로), `entries`(항목마다 `role`(`db`/`wal`/`shm`/`journal`/`meta`)·`src`(원래 절대 경로)·`name`(격리 안 이름, 단일 basename)). 디렉터리를 fsync 한다.
  2. 대상을 순서대로 하나씩 rename 해 넣는다. 역할과 순서는 `boot/cli.py` 의 `quarantine_roles()` 한 곳에 있다: DB → `-wal` → `-shm` → `-journal` → 메타 디렉터리. 없는 대상은 건너뛴다.
  3. fsync(디렉터리) 뒤 `.partial` 을 `<db>.stale-<ts>/` 로 rename 한다. 이 rename 이 격리의 완료 표시다.
  - 도중에 죽으면 `.partial` 이 남는다. 다음 부팅은 판정 전에 이를 마저 끝낸다(멱등). **manifest 는 '무엇을 옮기려 했는지'의 기록일 뿐 이동 명령이 아니다.** 재개는 현재 `--db`·`--meta-path` 에서 다시 유도한 대상과 manifest 를 대조하고, **전체를 먼저 검증한 뒤** 원래 자리에 남은 대상만 옮기고 3 을 한다. 하나라도 어긋나면 아무것도 옮기지 않고 exit 2:
    - `.partial`·manifest 자체가 심볼릭 링크이거나 디렉터리·정규 파일이 아님, JSON·스키마·`version` 불일치, `db` 가 이번 DB 가 아님
    - 항목의 역할이 모르는 값·중복·순서 어긋남, `src` 가 그 역할의 유도 경로가 아님, `name` 이 그 경로의 basename 이 아님(`/`·`..`·빈 문자열·예약 이름 포함)
    - 메타 항목의 `src` 가 이번 `--meta-path` 와 다름 → 원래 `--meta-path` 로 다시 실행하라고 안내한다
    - 각 항목이 원래 자리·격리 안 중 **정확히 한 곳**에 있지 않음(둘 다 없음 = 손실, 둘 다 있음 = 그 사이 누가 새 파일을 만듦), 그 파일이 링크임
    - manifest 에 **없는** 역할의 파일이 원래 자리나 격리 안에 있음. 재개는 manifest 항목만이 아니라 `quarantine_roles()` 의 모든 역할을 양쪽에서 본다. 자동으로 보충해 옮기지 않는다(WAL 항목이 빠진 manifest 로 DB 만 격리하면 DB 와 WAL 이 갈라진다, 리뷰 재현). 처음부터 없던 역할은 문제없다
    - `.partial` 안에 manifest 에 없는 파일이 있음, 완료 이름 `<db>.stale-<ts>/` 가 이미 있음
  - manifest 를 쓰기 전에 죽었으면(manifest 없음, 임시 파일만 있음) 옮긴 파일이 없으므로 `.partial` 만 지운다. 그 밖의 내용이 있으면 exit 2.
  - 이 검증은 사고·손상(중단, 운영자 실수, 디스크 손상) 대비다. 볼륨 쓰기 권한을 가진 공격자에 대한 인증 수단이 아니다.
  - 격리를 마친 뒤 설치 전에 죽으면 로컬 DB·메타가 없으므로 다음 부팅은 `fresh` 로 복원한다. 설치 뒤 exec 전에 죽으면 D-14 와 같은 상태(DB 있음, 메타 없음)가 되어 `--on-unknown restore` 면 한 번 더 격리·복원한다. 어느 kill 지점에서도 DB 와 메타가 다른 격리 디렉터리로 갈라지지 않는다(테스트로 고정).
  - rename 은 같은 파일시스템 안에서만 원자적이다. 메타 디렉터리가 DB 와 다른 파일시스템에 있으면 복원 전에 거부한다(exit 2).
- `--on-unknown keep-local` : 로컬을 그대로 두고 진행하되, stderr 와 헬스 상태에 `unknown_at_boot` 를 남긴다. **원격 조회가 성공했을 때만** 통한다. 원격 조회 실패면 정책과 무관하게 거부한다(D-12, S3 장애 중 옛 볼륨 재부팅 방지).
- `--adopt-existing` : 기존 DB 를 처음 Litestream 에 올릴 때 쓴다. 정확히 (로컬 DB 있음, 로컬 메타 없음, 원격 조회 성공·빈 목록) 일 때만 `adopt` 로 진행하고, 그 조합 밖에서는 효과가 없다. `keep-local` 을 최초 도입 절차로 쓰지 않는다(D-11). 그 조합 밖에서는 무효과지만 **첫 도입 배포 뒤에는 끈다.** 판정 함수는 '도입했음'을 기억하지 않으므로, 켜 둔 채로 두면 나중에 메타와 복제본이 함께 사라진(예: prefix 오타) 상황에서 같은 조합이 다시 성립해 그 DB 를 새 복제본으로 올린다(D-13 과 같은 이유).
- `--init-new` : 생애 첫 배포에서 새 DB 로 시작할 때 쓴다. 정확히 (로컬 DB 없음, 로컬 메타 없음, 원격 조회 성공·빈 목록) 일 때만 `fresh/new_db` 로 진행하고, 없으면 같은 조합을 `unknown/no_replica_no_local` 로 거부한다. Litestream 은 복제본 경로·prefix 오타와 "복제본 없음"을 같은 빈 목록(rc 0, `[]`)으로 돌려주므로(#3 실측) 빈 목록만으로 새 DB 를 시작하면 오타 난 경로에 새 DB 를 복제해 기존 복제본을 버리게 된다. 그 조합 밖에서는 무효과지만, 판정 함수는 '첫 배포였음'을 기억하지 않으므로 나중에 DB·메타가 함께 사라지고 복제본 경로까지 틀리면 같은 조합이 다시 성립한다. 그래서 **첫 배포 뒤에는 끈다**(D-13 정정).
- **원격 TXID 조회**는 `litestream ltx -config <설정> -level all -json <db 경로>` 로 설정 파일의 복제본을 모든 레벨에 걸쳐 보고, 항목들의 `max_txid`(16자리 16진수) 최대값을 쓴다. 기본(`-level` 생략)은 L0 만 나열해 L0 가 사라진 복제본을 빈 목록으로 오판한다(0.5.17 `ltx -h` 와 실측). 구현은 `boot/litestream.py` 의 `remote_max_txid()`.
  - rc 0 + `[]` → `RemoteEmpty`. 0.5.17 은 복제본 경로가 없을 때와 비어 있을 때 모두 이 출력이라 둘을 구분할 수 없다(경로 오타도 빈 목록이다). 조회 모듈은 사실(빈 목록)만 돌려주고, 이를 어떻게 다룰지는 판정 정책이 정한다. 빈 목록의 처리는 아래 상태 규칙표를 따르며, 로컬 DB·메타가 모두 없을 때 새 DB 로 시작하려면 `--init-new` 가 필요하다(D-13).
  - rc ≠ 0(설정에 없는 DB·설정 파일 없음·YAML 오류·접근 불가 모두 rc 1, stderr `Error: ...`), 타임아웃(기본 30초), JSON·스키마 이상, 검증하지 않은 Litestream 버전(`litestream version` 이 `VERIFIED_VERSIONS`, 현재 0.5.17 밖) → `RemoteError`.
- **로컬 TXID 조회**는 로컬 메타 디렉터리(기본 `<db 디렉터리>/.<db 이름>-litestream/`, 설정의 `meta-path` 로 바뀜)의 `ltx/0/` 에서 고른 최신 LTX 파일 하나의 max TXID 를 쓴다. 구현은 `local_max_txid()`.
  - 후보: 파일 이름 `<min>-<max>.ltx`(16자리 소문자 16진수) 중 `max` 가 가장 큰 것. Litestream 0.5.17 이 자기 복제 위치를 정하는 `DB.MaxLTX()` 와 같은 선택이다. L0 보존 정리는 가장 새 L0 파일을 지우지 않으므로 업로드·압축·종료 뒤에도 남는다(소스 `EnforceL0RetentionByTime` 과 실측).
  - 검증: 그 후보 하나가 정규 파일(LTX 파일 자체의 심볼릭 링크는 거부하고, 상위 메타 디렉터리의 링크는 허용해 따라간다)이고 읽을 수 있으며, 이름이 1 ≤ min ≤ max 이고, LTX 헤더(superfly/ltx v0.5.2: 매직 `LTX1`, flags 는 `NoChecksum` 비트만, page size 512–65536 의 2의 거듭제곱, `[16:24]`·`[24:32]` 의 min/max)가 이름과 일치해야 한다. 어긋나면 낮은 후보로 내려가지 않고 `None`(로컬 메타 없음 → `unknown`)이다. 낮은 후보를 쓰면 실제보다 오래된 위치를 믿게 된다.
  - 제한: 체크섬·페이지 전체 검증은 하지 않는다. Litestream 의 `DB.Pos()` 는 같은 파일을 `Decoder.Verify()` 로 끝까지 검증하므로, 헤더는 멀쩡하고 본문만 깨진 파일은 우리가 TXID 로 받아들이지만 Litestream 은 오류를 낸다. v0.1 범위 밖이다.
  - `litestream ltx <db 경로>` 는 로컬이 아니라 복제본을 나열하므로 쓰지 않는다.
- **복원 호출**은 `litestream restore -config <설정> -json -integrity-check quick -o <새 경로> <db 경로>` 다. 출력 경로는 아직 없어야 하고(`-force` 를 쓰지 않는다) `-if-db-not-exists` 는 쓰지 않는다. rc 0, `-json` 요약의 `txid`, 출력 파일이 비어 있지 않음을 모두 확인해야 성공이다. 기본 타임아웃 600초. 파일 교체·격리는 호출자(#4)가 한다. 구현은 `restore()`.
- 판정에 쓰는 정보와 비교 규칙은 `boot/decide.py` 의 순수 함수 `decide()` 하나에 모은다. 입력은 (로컬 존재 여부, 로컬 메타 TXID|None, 원격 조회 결과, `on_unknown`, `adopt_existing`, `init_new`)이고, 원격 조회 결과는 실패(`RemoteError`)·빈 목록(`RemoteEmpty`)·최대 TXID(`RemoteTxid`) 세 타입으로 구분한다. 원격 결과는 정확한 타입으로 한 번 검증해 내부 태그로 정규화하고, TXID 는 내장 `int` 만 받는다(서브클래스는 비교를 바꿔 판정을 우회할 수 있다). 출력은 (상태, 조치, 사유 코드, 사람용 사유 한 줄)이다. 표 기반 단위 테스트(`tests/test_boot_decide.py`)로 모든 조합을 고정한다.

상태 규칙 (위에서부터 처음 맞는 줄. `*` 는 무관):

| 로컬 DB | 로컬 TXID | 원격 | 상태 | 사유 코드 |
|---|---|---|---|---|
| 없음 | 값 있음 | * | `unknown` | `stale_meta` — DB 는 없는데 메타만 남음 |
| * | * | 실패 | `unknown` | `remote_error` — DB 가 없어도 복제본 유무를 모르므로 새 DB 로 시작하면 원격을 덮을 수 있다 |
| 없음 | 없음 | 빈 목록, `init_new` | `fresh` | `new_db` — 생애 첫 배포(D-13) |
| 없음 | 없음 | 빈 목록 | `unknown` | `no_replica_no_local` — 복제본 경로 오타와 구분되지 않는다(D-13) |
| 없음 | 없음 | n | `fresh` | `restore_from_remote` |
| 있음 | 없음 | 빈 목록, `adopt_existing` | `adopt` | `adopt_existing` — 기존 DB 최초 도입(D-11) |
| 있음 | 없음 | 성공 | `unknown` | `no_local_meta` |
| 있음 | t | 빈 목록 | `unknown` | `remote_empty` — 빈 목록을 '로컬 유지'로 보지 않는다(설정 오류·경로 오타일 수 있다) |
| 있음 | t | n, t ≥ n | `match` | `local_current` — 복제되지 않은 로컬 커밋 보존(L5) |
| 있음 | t | n, t < n | `unknown` | `remote_ahead` |

조치 규칙:

| 상태 | `refuse`(기본) | `restore` | `keep-local` |
|---|---|---|---|
| `fresh`, 빈 목록(`init_new`) | `PROCEED` | `PROCEED` | `PROCEED` |
| `fresh`, n | `RESTORE` | `RESTORE` | `RESTORE` |
| `match` | `PROCEED` | `PROCEED` | `PROCEED` |
| `adopt` | `PROCEED` | `PROCEED` | `PROCEED` |
| `unknown`, 원격 n 있음 | `REFUSE` | `QUARANTINE_AND_RESTORE` | 로컬 DB 있으면 `KEEP_LOCAL`, 없으면 `REFUSE` |
| `unknown`, 원격 빈 목록 | `REFUSE` | `REFUSE`(복원할 것이 없다) | 로컬 DB 있으면 `KEEP_LOCAL`, 없으면 `REFUSE` |
| `unknown`, 원격 실패 | `REFUSE` | `REFUSE` | `REFUSE`(D-12) |

- `QUARANTINE_AND_RESTORE` 는 로컬(DB·메타)을 `<db>.stale-<ts>/` 로 옮긴 뒤 복원한다. `stale_meta` 도 남은 메타를 옮겨야 하므로 같은 조치다. `KEEP_LOCAL` 은 진행하되 `unknown_at_boot` 를 남긴다(§7). `REFUSE` 는 exit 2 다.
- 안전 불변식(테스트로 고정): 로컬 DB 가 있으면 `RESTORE` 는 나오지 않는다(덮어쓰기는 반드시 격리를 거친다). 원격 조회 실패면 `REFUSE` 만 나온다(D-12). `refuse` 면 `unknown` 은 모두 `REFUSE` 다. `adopt_existing` 이 켜져 있어도 원격에 복제본이 있거나, 원격 조회 실패거나, 로컬 메타가 있으면 `adopt` 는 나오지 않으며, 위 한 행 밖에서는 결과가 꺼졌을 때와 같다(D-11). 로컬 DB 가 없을 때 `PROCEED` 는 `init_new` 가 켜져 있고 원격이 빈 목록일 때만 나오며, `init_new` 는 그 한 행 밖에서 결과를 바꾸지 않는다(D-13).

### 4-4. 스파이크 guard.py 와의 차이 (고친 결함)
`docs/reference/guard_spike.py` 는 랩에서 D4(옛 볼륨 재부팅)를 막는 데 성공했다. 그러나 그대로 옮기면 안 된다. 교차 리뷰에서 지적됐고, 코드로 직접 확인한 결함이다.
- 원격 조회 실패와 빈 목록을 둘 다 "로컬 유지, exit 0"으로 처리한다 → v1 에서는 `unknown` 으로 판정한다.
- 메타가 없으면 원격으로 복원한다. 그러면 더 새로운 로컬을 버릴 수 있다 → v1 에서는 `unknown` 으로 판정한다.
- 격리를 파일별 rename 으로 하고 진행 표시가 없다. 중간에 죽으면 반쯤 옮겨진 상태가 남고 다음 부팅이 그것을 모른다 → v1 은 `.partial` 디렉터리와 manifest 로 진행 중임을 남기고, 다음 부팅이 판정 전에 마저 끝낸다(§4-3). 또 복원을 격리보다 먼저 끝내므로 복원이 실패하면 로컬은 바뀌지 않는다.
- L0 만 본다 → v1 은 `-level all` 을 쓴다.

### 4-5. 종료 코드
| 코드 | 뜻 |
|---|---|
| `0` | 진행. 실제로는 exec 로 프로세스가 바뀌므로 boot 가 0 을 돌려주는 일은 없고, 이후 종료 코드는 exec 된 명령의 것이다 |
| `2` | 거부: `unknown`(+ `refuse` 정책, 원격 조회 실패 등), DB 없이 사이드카만 남음, `.partial` 이 둘 이상·검증 실패, 격리 대상이 링크·예약 이름 충돌·다른 파일시스템, DB 경로가 정규 파일이 아님, 설치 자리가 비어 있지 않음, 상태 파일을 쓸 수 없음 |
| `3` | 무결성 실패: `PRAGMA quick_check` 가 `ok` 가 아니거나 DB 를 열 수 없음 |
| `4` | 복원 실패. 임시 복원 실패(`litestream restore` 실패·타임아웃·결과 이상)면 로컬은 바뀌지 않는다. 격리·설치 중 I/O 실패도 4 다. 격리 도중 실패해 `.partial` 이 남으면 다음 부팅이 재개한다. 격리 완료·설치 이후 실패는 남은 상태에 따라 다음 부팅이 fresh 복원(로컬 DB·메타 없음) 또는 D-14 복구(DB 있음·메타 없음 → `no_local_meta`)를 따른다 |
| `5` | 잠금 실패: 다른 boot 나 그것이 exec 한 명령이 잠금을 쥐고 있음, 잠금 파일을 만들 수 없음·링크·정규 파일 아님·잠그는 사이 바뀜 |
| `64` | 사용법 오류: 인자 오류, `--` 뒤 명령 없음, `--db`·`--meta-path` 가 실제 경로가 아님(D-15: `..` 포함, 파일 이름으로 끝나지 않음, 부모가 없거나 디렉터리가 아니거나 링크를 거침), POSIX `fcntl` 이 없는 플랫폼 |
| `127` | exec 실패: `--` 뒤 명령을 실행할 수 없음(없는 명령, 실행 권한 없음) |

## 5. Django 쪽 구성

```
django_sqlite_ops/
├── boot/            Django import 금지. 표준 라이브러리 + subprocess(litestream)만
│   ├── __main__.py
│   ├── decide.py    판정 순수 함수
│   ├── litestream.py  ltx/restore 호출과 출력 파싱(버전 고정 테스트)
│   └── lock.py
├── database.py      권장 설정 표 + sqlite_database() (§6-0). 표준 라이브러리만 import
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

## 6. 권장 설정, 진단과 체크

### 6-0. 권장 설정 (`database.py`)
- `settings.py` 에서 부르는 함수다. 그래서 **Django 를 import 하지 않는다**(표준 라이브러리만). 결과는 그대로 `DATABASES` 에 넣는 dict 다.
  ```python
  from django_sqlite_ops.database import sqlite_database

  DATABASES = {
      "default": sqlite_database(BASE_DIR / "app.sqlite3", profile="single-server"),
  }
  # pragmas={...} 로 개별 PRAGMA 를 덮거나 더하고(값이 None 이면 뺀다),
  # options={...} 로 OPTIONS 키를 더하거나 덮는다. options 의 init_command 는 거부한다(pragmas 를 쓴다)
  ```
- 권장값 표는 이 모듈 안의 상수 하나가 정본이다. 정적 체크(§6-1)와 `sqlite_doctor`(§6-2)는 같은 표를 읽어 비교한다. 표와 문서가 어긋나지 않게 문서 표는 테스트로 대조한다.
- 값마다 근거 등급을 단다. **실측 근거가 있는 값만 기본으로 켠다.** 근거가 없는 값은 랩에서 재기 전까지 후보로만 문서에 둔다.

  | 항목 | 권장값 | 근거 | 기본 적용 |
  |---|---|---|---|
  | `transaction_mode` | `IMMEDIATE` | 실측: gunicorn 4 워커·스레드 8, 1,200건 쓰기에서 `database is locked` 0건 (`litestream-django-scenarios.md`) | 켬 |
  | `journal_mode` | `WAL` | 실측: 위와 같음. Litestream 이 없는 개발·테스트 환경과 동작을 맞춘다 (같은 문서 관찰 2) | 켬 |
  | `synchronous` | `NORMAL` | 실측 조건에 포함(같은 문서). WAL 에서 커밋 내구성은 체크포인트 전 전원 손실 시 마지막 트랜잭션이 빠질 수 있다 — 문서에 명시 | 켬 |
  | `busy_timeout` | 5000ms | 실측 조건에 포함(같은 문서). Python `timeout` 기본 5초와 같은 값으로 맞춘다 | 켬 |
  | `foreign_keys` | `ON` | Django sqlite3 백엔드가 이미 켠다 — 생성 함수는 건드리지 않는다 | 해당 없음 |
  | `temp_store`, `mmap_size`, `cache_size`, `journal_size_limit` | 후보 | 랩 벤치(§10, 2026-10-08): 개선이 재현된 후보 없음. `cache_size=-65536` 단독은 반복마다 나빠짐(처리량 −8~−12%, p99 +17~+27%, `CONN_MAX_AGE=0`)([`lab-2026-10-08.md`](research/lab-2026-10-08.md#pragma-벤치)). 재측정(#26, `CONN_MAX_AGE` 0·None × WAL 이 자라는 부하): 재현된 차이 없음, `cache_size` 악화도 재현 안 됨(소음 큼, #27). `journal_size_limit` 은 Litestream 과 함께면 WAL 을 묶지 못함(관측: 그 변형의 `write` 10단계 중 4단계에서만 64 MiB 로 잘리고 다시 자람. SQLite 는 재시작 뒤 첫 커밋한 연결의 한도로만 자르고 Litestream 은 체크포인트 직후 자기 연결로 쓴다 — 구조는 코드상 확인, 대개 Litestream 이 먼저 커밋한다는 것은 추정)([`bench-2026-10-08.md`](research/bench-2026-10-08.md)) | 끔 |
  | `wal_autocheckpoint` | 건드리지 않음 | Litestream 이 체크포인트를 관리한다. 바꾸면 복제와 충돌할 수 있다 [확인 필요] | 끔 |
  | `CONN_MAX_AGE` | 프로필별 | VFS 별칭은 `None` 필수(실측 1,008ms → 1.7ms, WSGI). ASGI 동기 뷰(일반 별칭)에서는 `None` 이어도 요청마다 새 연결이고(요청마다 새 스레드), 닫히지 않은 연결이 fd 를 쌓아 500 이 났다(재현함, [`bench-2026-10-08.md`](research/bench-2026-10-08.md#결과-연결-재사용)). ASGI 의 VFS 별칭은 미검증. 일반 별칭의 `None` 은 ASGI 로 판단되면 W005 로 알린다(§6-1) | 2단계(VFS 프로필) |

- `init_command` 는 연결마다 실행된다. 그래서 가벼운 PRAGMA 만 넣고, 데이터나 스키마를 바꾸는 문장은 넣지 않는다.


### 6-1. 정적 체크 (`manage.py check`, DB 를 열지 않음)
기준값은 §6-0 의 표에서 읽는다. `sqlite_database()` 를 쓰지 않은 설정(직접 쓴 dict, dj-lite 결과)도 같은 기준으로 검사한다.

대상은 `ENGINE == "django.db.backends.sqlite3"` 인 별칭이다. 프로필은 `settings.SQLITE_OPS["PROFILE"]`(없으면 `single-server`)이다. `obj` 는 별칭 이름이다. 체크는 `ready()` 에서 등록만 하고, 설정만 읽으며 DB 연결을 만들지 않는다(테스트로 확인: 체크 전후 `connection is None`, DB 파일 생성 없음).

| ID | 조건 | 수준 |
|---|---|---|
| `sqlite_ops.E001` | `SQLITE_OPS` 가 dict 가 아니거나 `PROFILE` 이 알 수 없는 이름. 이때는 이 오류 하나만 내고 다른 체크는 건너뛴다 | Error (항상) |
| `sqlite_ops.E002` | `SQLITE_OPS["HEALTH"]`(§7)가 잘못됨: dict 가 아님, 모르는 키, `DATABASES` 가 비었거나 `DATABASES` 에 없는·sqlite3 가 아닌 별칭, 별칭 항목이 dict 가 아님·`litestream_config` 없음·`meta_path`/`litestream` 이 빈 값, `REFRESH` 가 양수 아님, `BACKLOG_GRACE` 가 음수(숫자가 아니거나 bool·무한대 포함). `HEALTH` 가 없으면 검사하지 않는다. 검증은 `health.parse_config()` 한 곳이고 뷰도 같은 결과를 쓴다. `obj` 는 해당 별칭(설정 전체 문제면 없음) | Error (항상, ASGI 포함) |
| `sqlite_ops.W001` | sqlite3 별칭의 `OPTIONS.transaction_mode` 가 `IMMEDIATE` 가 아님(없음 포함, 대소문자 무시). 읽기 전용·VFS 별칭은 건너뛴다 | Warning (`--deploy` 일 때만) |
| `sqlite_ops.W002` | `init_command` 에서 `journal_mode` 가 `WAL` 로 설정되지 않음, 또는 판정할 수 없음. Django 처럼 `;` 로 나눈 문장마다 SQL 주석을 지우고 보며, 마지막으로 확정된 설정값을 쓴다. 대소문자·공백·인용 식별자(`"…"`·`` `…` ``·`[…]`)·`main.` 접두는 인정하고 다른 스키마는 세지 않는다. `journal_mode` 를 언급하지만 형식을 확정할 수 없는 문장이 하나라도 있으면 판정할 수 없다고 경고한다. 메모리 DB·읽기 전용·VFS 별칭은 건너뛴다 | Warning (`--deploy`) |
| `sqlite_ops.W003` | 이름이 Litestream VFS(`vfs=litestream` 이 든 `file:` URI)인 별칭의 `CONN_MAX_AGE` 가 `None` 이 아님(키 없음 = Django 기본 0 포함), 그리고 ASGI 근거가 없음(WSGI 로 판단, 아래 'ASGI 판단'). ASGI 는 미검증이라 내지 않는다(교차 리뷰) | Warning (항상) |
| `sqlite_ops.W005` | VFS 가 아닌(`vfs=litestream` 이 없는) sqlite3 별칭의 `CONN_MAX_AGE` 가 `None`(키가 있고 값이 `None`), 그리고 ASGI 근거가 있음. 메모리·읽기 전용·W004 대상 별칭도 VFS 가 아니면 본다. 메시지에 판단 근거를 적는다. 0·양수·키 없음에는 내지 않는다 | Warning (항상) |
| `sqlite_ops.W004` | `NAME` 으로 별칭 역할(쓰기·읽기 전용·메모리·VFS)을 판정할 수 없음: URI 쿼리 키 `mode`·`immutable`·`vfs` 중복, `mode` 가 `ro`·`rw`·`rwc`·`memory` 밖, `immutable` 이 SQLite 불리언 표기 밖, 디코딩한 파일명이나 쿼리 키·값(역할과 무관한 키 포함)에 NUL, `file://` 의 authority 가 빈 값·`localhost` 가 아님. 이 별칭에는 W001·W002 를 내지 않는다 | Warning (`--deploy`) |

- **ASGI 판단**(W003·W005 공통, `checks._asgi_evidence()`): 서버 실행 명령은 설정에 없으므로 설정에서 정적으로 보이는 근거만 쓴다. 한 설정에는 판단이 하나라 W003(WSGI 일 때만)과 W005(ASGI 일 때만)가 서로 반대로 판단하지 않는다(테스트 `test_w003_w005_never_disagree_in_one_settings`).

  | 근거 | 판단 | 이유 |
  |---|---|---|
  | `ASGI_APPLICATION` 이 참값 | ASGI | channels·daphne 가 쓰는 설정. Django 자체 설정이 아니므로 넣었다면 ASGI 서버를 쓰려는 것이다 |
  | `SQLITE_OPS` 에 `"PROFILE"` 키(값 무관 — 모르는 값은 E001 이 먼저 막는다) | ASGI | 두 배포 프로필은 ASGI 서버로 띄운다: 문서의 앱 프로세스가 `uvicorn --workers 1`·`uvicorn --workers N` 이고 entrypoint 가 `exec uvicorn proj.asgi:application` 이다(`docs/profiles/*.md`, §8). 기본값으로 정해진 프로필(키 없음)은 근거가 아니다 — 모든 설정에 적용되기 때문이다 |
  | 위 둘 다 없음 | WSGI | 종전 W003 규칙과 같다 |
  | `WSGI_APPLICATION` | 근거로 쓰지 않음 | `startproject` 의 settings 템플릿이 늘 넣는다(Django 의 기본값은 `None`) |
  | `INSTALLED_APPS` 의 `daphne`·`channels` | 근거로 쓰지 않음 | 설치만으로는 실행 서버를 알 수 없다. 프로필 문서의 channels 구성은 `ASGI_APPLICATION` 을 함께 두므로 첫 행이 잡는다 |

  | 사례 | 실제 서버 | 판단 | 결과 |
  |---|---|---|---|
  | 프로필 문서 그대로(`ASGI_APPLICATION` + `PROFILE`) | uvicorn | ASGI | 맞음 |
  | channels 없이 `PROFILE` 만 두고 `ASGI_APPLICATION` 을 뺌(single-server 문서가 허용) | uvicorn | ASGI | 맞음 |
  | `PROFILE`·`ASGI_APPLICATION` 둘 다 없음 | uvicorn·daphne·hypercorn | WSGI | **미탐**: W005 가 나지 않고, VFS 별칭이면 W003 이 난다(ASGI 의 VFS 동작은 미검증이라 그 W003 은 근거가 없다). 정적으로 알 수 없어 남긴다 |
  | `PROFILE` 또는 `ASGI_APPLICATION` 이 있음 | gunicorn(WSGI) | ASGI | **오탐**: W005 가 나고(WSGI 에서는 `None` 이 재사용된다), VFS 별칭의 W003 이 빠진다. `SILENCED_SYSTEM_CHECKS` 로 끈다 |
  | 웹소켓만 daphne, HTTP 는 gunicorn(혼합) | 둘 다 | ASGI | 프로세스마다 다르다. ASGI 프로세스 기준으로 경고한다(오탐일 수 있다) |
  | 아무 근거 없음 | gunicorn | WSGI | 맞음 |

- W005 근거: ASGI(uvicorn, Django 6.1) 동기 뷰에서 일반 별칭의 `None` 은 DB 요청마다 새 연결(연결 생성 ≈1.0/요청)이고 요청 끝에 닫히지 않아 열린 DB fd 가 810–982(한도 1024)까지 쌓였으며 `unable to open database file` 500 이 났다(재현함, [`bench-2026-10-08.md`](research/bench-2026-10-08.md#결과-연결-재사용)). 원인이 fd 고갈이라는 것은 추정이다(SQLite 메시지에 errno 가 없다). 그래서 메시지는 fd 고갈을 "suspected cause" 로만 쓴다. 권장은 `0` 또는 키 삭제(기본 0)이고, `0` 일 때 재사용 이득도 없었다. 양수 `CONN_MAX_AGE` 는 재지 않아 경고하지 않는다(§12). fd 한도를 올려도 권장은 같다: 열린 파일 한도를 1,048,576 으로 올리고 동시 16 으로 30분(요청 118만 건) 돌리면 `None` 도 오류 없이 돌았고 fd·RSS 는 요청 수에 비례해 늘지 않고 멈췄지만(fd 882–1,531, 앱 RSS 310–465 MiB), `0`(fd 74–91, RSS 67–83 MiB)보다 fd 약 15배·RSS 약 5.6배를 쓰고 재사용 이득은 여전히 없었다(재현함, [`fd-soak-2026-10-09.md`](research/fd-soak-2026-10-09.md)). 그 정상 상태 수준이 기본 소프트 한도 1024 를 넘는 것이 #26 의 500 의 원인이라는 것은 추정이다. hint 문구는 이 결과와 모순되지 않아 그대로 둔다.
- W001 은 권고 수준이다. Django 는 `DEFERRED`·`EXCLUSIVE`·`IMMEDIATE` 를 모두 허용하므로 오류로 올리지 않는다(교차 리뷰).
- 원칙: **판정할 수 없으면 경고한다.** 정적 체크는 SQL 파서가 아니므로 모르는 형식을 조용히 건너뛰지 않는다. 체크 결과는 실제 Django 연결의 `PRAGMA journal_mode` 와 대조하는 표 테스트로 고정한다(`tests/test_checks.py`).
- 원칙(별칭 역할): **역할(쓰기·읽기 전용·메모리·VFS)을 확정할 수 없으면 쓰기 권고(W001·W002)를 내지 않고 W004 를 낸다.** 잘못된 권고로 연결을 깨뜨리지 않으면서 문제를 조용히 넘기지도 않는다. W004 는 쓰기 설정을 넣어도 사라지지 않는다. SQLite 의 중복·비표준 해석을 흉내 내지 않는다(중복 `mode` 는 순서에 따라 읽기 전용이 되거나 연결 오류가 난다 — 재현함).
- 별칭 이름 판별: `NAME` 이 PathLike 면 `os.fspath()` 로 바꾼다. `file:` URI 는 SQLite 규칙대로 파일명(첫 `?`·`#` 앞)과 쿼리(`?` 뒤 `#` 앞)로 먼저 나눈 뒤 파일명과 쿼리 키·값을 퍼센트 디코딩한다(https://www.sqlite.org/uri.html). NUL 이 있으면 SQLite 는 그 앞까지만 읽어 `mode%00x=ro` 같은 키가 실제로는 `mode` 로 작동한다(재현함, 리뷰 3). NUL 해석을 흉내 내지 않고 W004 로 보낸다. `file:` 바로 뒤가 `//` 면 다음 `/` 까지(쿼리·프래그먼트 구분자보다 먼저)가 authority 다. SQLite 는 빈 값과 정확히 `localhost` 만 받고(대소문자 구분, 디코딩 안 함), 그 밖은 `invalid uri authority` 로 연결이 실패한다(재현함, #6 리뷰 1). 로컬 authority 는 떼어 내고 경로를 보며, 그 밖은 W004 로 보낸다. Windows 의 `SQLITE_ALLOW_URI_AUTHORITY` UNC 해석은 흉내 내지 않는다. 판정 순서: W004 조건 → 메모리 → 읽기 전용 → VFS → 쓰기.
  - 메모리 DB: `NAME == ":memory:"`, 디코딩한 URI 파일명이 정확히 `:memory:`, 또는 `mode=memory`. `file::memory:backup.sqlite3` 는 실제 파일이다(재현함). W002 만 건너뛴다.
  - 읽기 전용: `mode=ro` 또는 `immutable` 참값. `immutable` 은 `sqlite3_uri_boolean` 표기 중 참 `1`·`yes`·`true`·`on`, 거짓 `0`·`no`·`false`·`off`(대소문자 무시)만 받는다(https://www.sqlite.org/c3ref/uri_boolean.html). SQLite 는 "0 이 아닌 숫자로 시작"도 참으로 보지만 이 체크는 W004 로 보낸다. 쓰기 권고(W001·W002)가 의미 없고, W002 안내를 따르면 `mode=ro` 는 `attempt to write a readonly database` 로 깨지고 `immutable` 은 효과가 없다(재현함).
  - Litestream VFS: `vfs=litestream`. W001·W002 를 건너뛰고 W003 만 본다. VFS 의 PRAGMA 동작은 미검증이다.
  - 표 테스트(`NAME_TABLE`)는 행마다 체크 결과와 실제 연결의 `journal_mode`, 그리고 W001·W002 안내를 적용한 뒤의 체크·실제 결과를 함께 고정한다.
- 체크 태그는 `sqlite_ops` 다. Django 6.1 은 `--database` 없이 돌면 `database` 태그 체크를 빼고(6.1.2 `django/core/checks/registry.py:89-93`), 5.2 는 빼지 않는다(5.2.18 같은 파일 `run_checks` :72-96 에 그 분기가 없다). 두 지원 버전에서 기본으로 돌도록 자체 태그를 쓴다.

뺀 것과 그 이유:
- "다중 프로세스인데 InMemory 레이어 사용": check 시점에는 프로세스 수를 알 수 없다. wireview W006 이 이미 이 경우를 다룬다.
- NATS 토큰 누락 체크: `connect_options` 로도 인증을 넣을 수 있어 오탐이 난다.
- `timeout` 미명시 경고: Python sqlite3 의 기본값이 이미 5초다.
- SQLite 버전 하한 검사: Django 가 이미 한다.

### 6-2. `sqlite_doctor` (명시 실행, 실제 연결)
로직은 `django_sqlite_ops/doctor.py`, 명령(`management/commands/sqlite_doctor.py`)은 옵션·출력·종료 코드만 맡는다. 옵션: `--database ALIAS`(반복), `--litestream-config PATH`, `--litestream BIN`, `--json`.

- **연결하면 `init_command` 가 실행된다.** 그래서 진단이 상태를 바꿀 수 있다(`journal_mode=WAL` 은 지속된다, 교차 리뷰). 명시 실행 명령으로만 두고 도움말·README 에 적는다.
- **실제 연결 대상**: 별칭마다 Django 의 `connection.get_connection_params()["database"]` 를 한 번 구해 역할 판별·존재 검사·크기·마운트·복제 대조·채널 규칙에 모두 쓴다. 이 함수는 연결을 열지 않는다(5.2.18·6.1.2 소스 확인: `NAME` 뒤에 `OPTIONS` 를 병합해 kwargs 를 만들고 `transaction_mode`·`init_commands` 속성만 정한다). 그래서 `OPTIONS["database"]` 가 `NAME` 을 덮으면 그 경로를 본다(#6 리뷰 1). 이 함수가 예외(`NAME` 없음, 잘못된 `transaction_mode`)를 내면 `error`.
- **DB 별칭**(sqlite3 엔진, 역할은 `checks._role()` — 단 `vfs=litestream` 이면 `mode=ro` 가 있어도 VFS 로 본다):
  - DB 파일이 없으면 **연결하지 않고** `error`(연결하면 빈 DB 가 생긴다, §4-1). 정규 파일이 아니어도 `error`. VFS 별칭(확장 필요)과 역할 `unknown` 별칭은 연결하지 않고 `unknown`. 메모리 DB 는 연결하되 "메모리 DB" 로만 적는다.
  - 사이드카 정책: 쓰기 별칭은 앱 연결과 같으므로 연결한다(WAL DB 면 `-wal`·`-shm` 이 생길 수 있다 — README 주의). 읽기 전용 별칭은 DB 헤더(오프셋 18·19, 2 = WAL, https://www.sqlite.org/fileformat2.html)가 WAL 이면 **`-shm` 유무와 관계없이** 연결하지 않고 `unknown`. WAL 읽기는 SQLite 가 본래 `-shm` 에 쓰고 `-wal` 을 만들 수 있다(재현함: 사이드카 없음 → `-wal`·`-shm` 생성, 빈 `-shm` → 32768바이트, 열린 쓰기 연결·비정상 종료 뒤 남은 `-shm` → 해시·mtime 변경, #6 리뷰 1·2). 읽기 전용 WAL 복제본은 쓰기 쪽 별칭이나 `immutable` URI 로 진단한다. 헤더는 `O_RDONLY` 로 읽는다. 별칭이 이미 `immutable` 참값이면 사이드카를 만들지 않으므로(실측) 연결한다. `immutable=1` 을 붙이거나 생긴 파일을 지우지 않는다.
  - 파일 DB 는 연결 전에 파일·`-wal` 크기를 재고, 연결 뒤 실제 `transaction_mode`(Django 연결 속성), §6-0 표의 PRAGMA 전부, `foreign_keys`, `sqlite_version()` 을 읽는다. 표와 다르면 `warn`. 읽기 전용 별칭은 쓰기 권고(`transaction_mode`·`journal_mode`·`synchronous`)를 비교하지 않는다(§6-1 과 같은 원칙). 값 매핑(`synchronous` 0–3 → OFF/NORMAL/FULL/EXTRA)은 `doctor._SYNCHRONOUS` 한 곳에 둔다. 연결 실패는 `error` 한 줄.
- **마운트**: DB 파일(없으면 존재하는 상위 디렉터리)의 실제 경로로 파일시스템 종류를 찾는다. Linux 는 `/proc/self/mountinfo` 의 가장 긴 마운트 지점(같으면 나중 줄), macOS 는 `statfs(2)`(ctypes), 그 밖의 POSIX 는 `mount` 출력. 네트워크 FS(nfs·nfs4·cifs·smb*·smbfs·afpfs·webdav·sshfs·9p·ceph·glusterfs·lustre 등)는 `warn`(https://www.sqlite.org/useovernet.html), 아는 로컬 FS(ext4·xfs·btrfs·zfs·apfs·overlay·tmpfs 등)는 `ok`, 그 밖은 `unknown`.
- **Litestream 설정**(`--litestream-config` 가 있을 때만. 없으면 `ok` "skipped"): `litestream databases -config C -json` 으로 DB 목록을 읽는다(YAML 파서를 넣지 않는다). 버전 검사·타임아웃은 boot 헬퍼(`boot/litestream.py` 의 `config_databases`)를 쓴다. 0.5.17 은 경로를 절대 경로로 내보내며 `..` 는 문자열로 접고 링크는 남긴다. 상대 경로와 `dir:` 항목은 Litestream 작업 디렉터리 기준이 되므로(`dir:` 항목은 작업 디렉터리 자체로 나온다, 실측) 새 임시 디렉터리에서 실행해 그 아래로 풀린 항목을 `warn` 으로 골라낸다. 그래서 `--litestream-config` 와 경로 구분자가 든 `--litestream` 은 실행 전에 호출자 cwd 기준 절대 경로로 바꾼다(PATH 로 찾는 이름은 그대로).
  - 대조는 실제 경로(`realpath`)로 한다(D-15). 설정 경로가 실제 경로가 아니면 `warn` — boot 가 거부할 구성이다.
  - 쓰기 별칭인데 설정에 없음 → `warn`(복제되지 않음). channels-lite 전용 채널 DB(앱 DB 와 다른 실제 파일)는 §8 규칙상 복제에서 빼므로 이 검사에서 제외한다. 앱 DB 와 파일을 공유하면 제외하지 않는다. 설정에 있는데 `DATABASES` 에 없음 → `ok`(정보). 실행·파싱 실패(설정 파일 없음, 깨진 YAML, 바이너리 없음, 검증 안 된 버전) → `error`.
- **채널 레이어**: `settings.CHANNEL_LAYERS` 를 읽기만 한다(`channels` 를 import 하지 않는다). 백엔드 경로로 종류를 알아보고 §8 표의 의미론을 한 줄로 요약한다. InMemory 는 프로필이 `single-server-multiproc` 이면 `warn`(프로필이 다중 프로세스를 명시하므로). channels_redis pub/sub 은 의미론 미측정이라 `unknown`, 모르는 백엔드도 `unknown`. channels-lite 는 §8 규칙대로 채널 DB 가 앱 DB(`default` 별칭이거나 다른 별칭과 같은 실제 파일)면 `warn`, Litestream 설정이 있고 채널 DB 가 복제 대상이면 `warn`. channels-lite aio 레이어를 쓰거나 `PATCH_CHANNELS_LITE_AIO` 가 켜져 있으면 §9 패치 상태를 `patch` 항목으로 낸다(적용됨 `ok`, 꺼짐·게이트 밖·미설치 `warn`). 이 경우에만 channels-lite 를 import 한다(DB 는 열지 않는다).
- **수준**: `ok` · `warn` · `error` · `unknown`. 판정할 수 없으면 숨기지 않고 `unknown` 으로 적는다.
- **종료 코드**: 0 문제 없음 · 1 경고 또는 `unknown` · 2 오류. 명령은 0 이 아니면 `SystemExit(code)` 로 끝난다.
- **사람용 출력**: 섹션(`settings`·`database`·`mount`·`litestream`·`channels`)별 표, 줄 앞에 수준. 마지막 줄 `summary: N warning(s), N error(s), N unknown -> exit N`.
- **`--json` 스키마(`version: 1`)**:
  ```json
  {
    "version": 1,
    "items": [
      {
        "section": "settings | database | mount | litestream | channels",
        "alias": "DB 별칭·채널 레이어 별칭, 또는 null",
        "key": "profile | role | file | wal | connect | alias | transaction_mode | journal_mode | synchronous | busy_timeout | foreign_keys | sqlite_version | filesystem | config | config_path | replicated | extra | backend | database",
        "level": "ok | warn | error | unknown",
        "value": "실제 값(문자열·정수) 또는 null",
        "expected": "권장값·기대값 또는 null",
        "message": "사람용 한 줄"
      }
    ],
    "summary": {"ok": 0, "warn": 0, "error": 0, "unknown": 0},
    "exit_code": 0
  }
  ```
  `key` 목록은 늘어날 수 있다. 소비자는 모르는 `key` 를 무시한다. 필드를 빼거나 뜻을 바꾸면 `version` 을 올린다.

## 7. 헬스

구현은 `django_sqlite_ops/health.py`(판정·스레드·뷰)와 `django_sqlite_ops/wal.py`(WAL 위치, 표준 라이브러리만). 사용자가 `health_view` 를 `urls.py` 에 붙인다. **헬스는 DB 연결을 열지 않는다**: 로컬 TXID 와 L0 의 WAL 위치는 메타 파일(`local_max_ltx()`·`ltx_wal_range()`), 원격은 `litestream ltx`(`remote_max_txid()`), 현재 커밋 위치는 `-wal` 파일을 읽기 전용으로 열어 헤더만 읽는다.

**설정**
```python
SQLITE_OPS = {
    "HEALTH": {
        # 별칭마다 litestream_config 필수, meta_path·litestream(바이너리) 선택
        "DATABASES": {"default": {"litestream_config": "/etc/litestream.yml"}},
        "REFRESH": 15,  # 초, 원격 조회 주기
        "BACKLOG_GRACE": 60,  # 초
    },
}
```
- 별칭의 DB 경로는 Django 실효 경로다(`OPTIONS["database"]` 포함, doctor 와 같이 `get_connection_params()` 로 읽고 연결하지 않는다). 쓰기 파일 DB 가 아니면(메모리·읽기 전용·VFS·판정 불가) 그 별칭은 `unknown`.
- D-15 실제 경로 규칙을 boot 의 `real_path()` 로 그대로 검사한다(부모에 링크·`..` 없음). DB 파일 자체가 링크여도, `meta_path` 가 규칙을 어겨도 `unknown`(사유 포함). 이때 원격 조회를 하지 않는다.
- 상대 경로(`litestream_config`·`meta_path`·경로 구분자가 든 `litestream`)는 설정을 읽을 때 프로세스 작업 디렉터리를 **앞에 붙이기만** 한다. `abspath()` 처럼 `..` 를 접으면 `link/../meta` 가 링크를 따라간 실제 위치가 아닌 다른 디렉터리로 바뀌어 D-15 검사를 우회한다(review-1 재현). 검사와 조회는 `real_path()` 가 돌려준 경로로 한다.
- 설정이 잘못되면 시스템 체크 `sqlite_ops.E002`(§6-1). 뷰는 스레드를 시작하지 않고 `unknown`(`code: invalid_config`)과 고정 사유를 돌려준다(설정값을 응답에 옮기지 않는다). `HEALTH` 가 없으면 체크하지 않고, 뷰는 `unknown`(`code: not_configured`)이다.

**상태 (별칭마다)** — 판정은 순수 함수 `alias_status(sample, now, refresh, grace)`(`now` 는 monotonic 초). 위에서부터 처음 맞는 것. `code` 는 응답의 고정 사유 코드다:

| 조건 | 상태 | `code` |
|---|---|---|
| 아직 첫 조회 전 | `unknown` | `not_checked` |
| 마지막 관측의 나이(monotonic) < 0 또는 > `REFRESH × 3`(갱신 스레드 멈춤 — 원격 조회가 타임아웃까지 매달린 경우 포함) | `unknown` | `stale` |
| 조회 전에 정해진 사유: 경로 규칙 위반 / 파일 DB 아님 / 갱신 중 예외 | `unknown` | `path_not_real` / `not_file_db` / `refresh_failed` |
| 부팅 상태 파일의 `unknown_at_boot` 가 참 | `unknown` | `unknown_at_boot` |
| 원격 조회 실패(`RemoteError`) | `unknown` | `remote_error` |
| 원격 빈 목록(`RemoteEmpty`) — 경로·prefix 오타와 구분되지 않는다 | `unknown` | `remote_empty` |
| 로컬 메타 없음(`None`) | `unknown` | `no_local_meta` |
| **원격 > 로컬** — 다른 기계가 같은 복제본에 쓰는 중일 수 있다(사유에 그렇게 적는다) | `unknown` | `remote_ahead` |
| 로컬 > 원격, 아직 올라가지 않은 로컬 TXID 가 기다린 시간 ≥ `BACKLOG_GRACE` | `backlog` | `local_ahead` |
| **WAL 근거 없음**(아래) + 보조 파일 시각이 앞선 관측보다 뒤로 감 | `unknown` | `file_time_backwards` |
| WAL 근거 없음 + 보조 파일 시각상 DB 가 L0 보다 새로운 상태 ≥ `BACKLOG_GRACE` | `backlog` | `db_not_replicated` |
| WAL 근거 없음 (그 밖 — `caught_up` 으로 단정하지 않는다) | `unknown` | `no_wal_evidence` |
| WAL 에 L0 뒤 커밋이 있는 상태 ≥ `BACKLOG_GRACE` | `backlog` | `db_not_replicated` |
| 로컬 > 원격, 그보다 짧음 | `caught_up`(`backlog_since` 표시) | `local_ahead_within_grace` |
| WAL 에 L0 뒤 커밋이 있고 그보다 짧음 | `caught_up`(`pending_since` 표시) | `db_changed_within_grace` |
| 그 밖(원격 == 로컬, WAL 커밋이 모두 L0 안) | `caught_up` | `in_sync` |

**DB → 로컬 L0: WAL 위치 근거 (review-2)** — 로컬 L0 는 Litestream 이 쓴다. `replicate` 가 죽거나 멈추면 로컬·원격 TXID 가 같은 채로 멈추므로 TXID 비교만으로는 그 뒤의 쓰기가 보이지 않는다(review-1 재현). 라운드 1 은 이를 파일 시각(`max(mtime(DB), mtime(DB-wal))` > 최신 L0 mtime)으로 봤지만 **파일 시각의 순서는 그 커밋이 L0 에 들어갔다는 증거가 아니다**: Litestream 은 L0 를 만들 때 WAL 페이지 맵(담을 범위)을 먼저 정하고 DB 를 복사한 뒤 파일을 닫으므로, 복사 중 커밋은 L0 밖인데 L0 mtime 은 그보다 늦다(review-2 재현: 8 × 1MiB 행 DB 에서 첫 L0 작성 중 커밋 → 첫 업로드 직후 SIGSTOP → 복원본 8행·원본 9행인데 라운드 1 판정은 `in_sync`, 3/3). 그래서 시계와 무관한 WAL 위치로 판정한다.
- **L0 가 담은 WAL 위치** — superfly/ltx v0.5.2 `ltx.go` `Header`(188–191)·`MarshalBinary`(293–296), big-endian: `[48:56]` WALOffset(int64), `[56:64]` WALSize(int64), `[64:68]` WALSalt1, `[68:72]` WALSalt2. Litestream 0.5.17 `db.go` `sync()` 는 `rd.pageMap()`(2080)으로 정한 범위를 `WALOffset: info.offset, WALSize: maxOffset - info.offset` 로 헤더에 쓰고(2146–2157) 그 **뒤에** 페이지를 복사한다(`writeLTXFromDB`, 2175). `verifyWithExecutor()`(1682, 1705–1707)는 다음 동기화를 `WALOffset + WALSize` 와 그 salt 에서 이어 간다. 즉 이 값이 "Litestream 이 L0 로 기록한 WAL 의 끝"이다. 실제 0.5.17 L0 에서 이 필드가 채워지고 현재 `-wal` 의 salt·마지막 커밋 끝과 맞는 것을 확인했다(테스트 `test_real_l0_records_wal_position`). 압축 파일(L1+)과 저널 모드는 0 이므로 L0 만 본다. 구현 `boot/litestream.py` `ltx_wal_range()`.
- **현재 WAL 의 커밋 위치** — [SQLite WAL 형식](https://www.sqlite.org/fileformat2.html#walformat): 헤더 32바이트(`[0:4]` 매직 0x377f0682/3 — 끝 비트가 체크섬 엔디언, `[4:8]` 3007000, `[8:12]` page size, `[16:24]` salt, `[24:32]` 헤더 체크섬), 프레임 헤더 24바이트(`[0:4]` page no, `[4:8]` 커밋 프레임이면 0 이 아님, `[8:16]` salt, `[16:24]` 누적 체크섬). `-wal` 을 `O_RDONLY|O_NOFOLLOW|O_NONBLOCK` 으로 열고 정규 파일만 읽는다. 구현 `wal.compare()`.
- **체크섬까지 검증한다.** 프레임은 salt 와 누적 체크섬이 맞을 때만 유효하다(SQLite 복구 규칙, Litestream `WALReader.readFrame` 과 같음). salt 만 보면 쓰는 중인 프레임(헤더만 쓰이고 페이지는 덜 쓰임)을 커밋으로 셀 수 있다. 비용은 L0 끝 **뒤의** 프레임만, 첫 커밋 프레임까지만 읽어 줄인다. 체크섬은 L0 끝 바로 앞 프레임 헤더의 체크섬 필드에서 이어 계산한다(Litestream `NewWALReaderWithOffset` 과 같은 방식). 한 번에 64 MiB 를 넘게 읽어야 하면(거대한 트랜잭션이 쓰이는 중) 판정하지 않는다.
- **판정** — 같은 salt: L0 끝 뒤에 유효한 커밋 프레임 → pending, 없음 → in_sync. 다른 salt(L0 이후 WAL 재시작): 현재 salt 의 커밋 프레임 → pending(새 세대의 커밋을 Litestream 이 아직 L0 로 쓰지 않음), 없음 → 근거 없음(체크포인트·재시작 사이를 Litestream 이 관측했는지 알 수 없다). `-wal` 없음·빈 헤더·헤더 체크섬 틀림·page size 불일치·L0 끝이 프레임 경계가 아님·`-wal` 이 L0 끝보다 짧음·L0 끝 직전 프레임의 salt 가 다름(덮어써짐)·L0 에 WAL 정보 없음 → 근거 없음.
- **읽는 도중 바뀐 WAL (review-3)** — 헬스는 잠금 없이 `-wal` 을 읽는다. Litestream 은 체크포인트를 막는 읽기 트랜잭션을 쥐고 같은 규칙으로 읽지만, 헬스는 DB 연결을 열지 않으므로 그 보호가 없다. L0 끝 직전 프레임을 읽은 직후 `PRAGMA wal_checkpoint(TRUNCATE)` 가 WAL 을 0바이트로 만들면 꼬리 스캔이 EOF 를 보고 **조회 전부터 있던** 미복제 커밋을 "없음"으로 읽어 `in_sync` 가 됐다(review-3 재현: 원본 2행·복원본 1행). 그래서 `wal.compare()` 는 PENDING 이 아닌 결과를 돌려주기 전에 처음 연 WAL 의 `(st_dev, st_ino)`(fd 와 경로 양쪽)·크기·헤더 32바이트(salt·체크섬 포함)를 다시 확인한다. 하나라도 바뀌었으면 **한 번만** 다시 읽고, 다시 읽어도 바뀌면 `NO_EVIDENCE`("the -wal changed during read")다. PENDING 은 L0 뒤의 유효한 커밋 프레임을 이미 읽었다는 근거라 그대로 돌려준다. 실측: Litestream 지속 실행 + 연속 쓰기(256KiB 트랜잭션, 쉼 없이 20초, 6,730회)에서 0.5초마다 판정해 `unknown` 이 한 번도 나오지 않았다(재시도로 흡수).
- pending 지속 시간은 `next_wal_pending_since()` 로 처음 관측한 때부터 monotonic 으로 세고, 최신 L0 (이름, WAL 끝, salt)가 바뀌면 다시 센다(Litestream 이 진행 중). 실측: Litestream 지속 실행 + 30ms 간격 쓰기(매번 연결 닫음) 30초, 0.5초마다 판정, grace 3초 → 55회 모두 `caught_up`, 쓰기를 멈춘 뒤 15초 → `in_sync`.
- Litestream 이 도는 동안에는 Litestream 이 연결을 쥐고 있어 `-wal` 이 남으므로 이 근거가 늘 있다. 드문 예외: TRUNCATE 체크포인트(기본 `truncate-page-n` 121359 페이지 ≈ 500MB 초과 시)로 `-wal` 이 0바이트가 되고 쓰기가 없으면 근거가 없다. 0.5.17 은 그때 새 L0 를 쓰지 않는다(`verifyWithExecutor` 의 `syncedToWALEnd` 분기(1719) → `sync()` 의 "no new wal pages" 건너뜀(2108)).

**보조 근거: 파일 시각** — WAL 근거가 없을 때만 쓴다(주로 Litestream 이 멈춘 뒤 앱의 마지막 연결이 닫혀 `-wal` 이 지워짐. 실측: uv Python 의 SQLite 3.53.1 은 지우고, Command Line Tools Python 의 3.51.0 은 남겼다).
- `next_pending_since(prev, files, observed, wall)`: `db_changed_at = max(mtime(DB), mtime(DB-wal))`(`-shm` 은 읽기에도 바뀌므로 뺀다)이 최신 L0 의 mtime 보다 늦은 것을 처음 관측한 때부터 센다. 최신 L0 (이름, mtime_ns)가 바뀌면 다시 센다.
- 이 근거는 **`backlog` 쪽으로만** 쓴다: grace 이상이면 `db_not_replicated`(사유에 "file times only"), 아니면 `no_wal_evidence` 로 `unknown`. 시각만으로 `in_sync` 를 내지 않는다.
- **파일 시각 역행(review-2 지적 2)**: 벽시계를 쓰는 유일한 판정 경로라, DB·`-wal`·최신 L0 의 mtime 중 하나라도 앞선 관측보다 이르면(`files_went_backwards()`, 파일마다 앞뒤 관측 모두에 있을 때만 비교) `file_time_backwards` 로 `unknown` 이고 앞선 pending 을 지우지 않는다. review-2 재현(`edge.py`: `-once` 뒤 커밋 → `db_not_replicated`, 그다음 DB·WAL mtime 을 L0 보다 120초 이전으로 → 라운드 1 은 `in_sync`)이 이제 `file_time_backwards` → 다음 관측 `no_wal_evidence` 다(테스트 `test_real_file_time_backwards_is_unknown`).
- 0.5.17 실측(라운드 1): Litestream 이 멈춰 있으면 앱의 마지막 연결이 닫힐 때의 체크포인트(읽기만 해도)와 `replicate -once` 의 종료가 DB mtime 을 바꾼다(SIGTERM 종료는 바꾸지 않았다). 그래서 이 경로의 `db_not_replicated` 는 "DB 가 쓰이는데 Litestream 이 진행하지 않음"이다. 다른 프로세스의 `touch`·복사·덮어쓰기는 이 경로를 틀리게 할 수 있다.

- "아직 올라가지 않은 로컬 TXID 를 처음 관측한 시점"(`backlog_since`)은 `next_backlog_since(prev, local, remote, observed, wall)` 로 갱신마다 계산해 프로세스 메모리에 둔다. 키는 그때의 로컬 TXID 이고, 원격이 그 TXID 에 닿으면 지금의 로컬 TXID 로 다시 센다. 로컬이 앞서 있지 않으면(같아짐·조회 실패 포함) 지운다. 라운드 2 에서 바꿨다: "로컬이 앞선 상태가 이어진 시간"으로 재면 쓰기가 계속되는 DB 는 매 관측 순간 로컬이 한 걸음 앞서 업로드가 따라와도 `backlog` 가 됐다(실측: 30ms 간격 쓰기, 0.5초마다 판정, grace 3초 → 55회 중 49회 `backlog`, 바꾼 뒤 0회). 업로드가 쓰기를 못 따라가면 기다린 시간이 쌓이므로 `backlog` 다. **재시작하면 초기화된다**(재시작 직후 오래된 backlog 가 `BACKLOG_GRACE` 동안 `caught_up` 으로 보인다. README 에 적음). 지속 시간은 관측 시점끼리의 차이라 판정은 최대 `REFRESH` 늦다.
- 그래서 `local_ahead` 의 backlog 시간은 **처음 관측한 미업로드 로컬 TXID 가 원격에 올라가기까지 기다린 시간**이다. 가장 오래된 미업로드 커밋의 엄밀한 나이나 로컬·원격의 총 격차가 아니다(원격이 그 TXID 에 닿을 때마다 새 관측으로 바뀐다 — review-3 §3).
- **지속 시간(grace·stale·pending)은 `time.monotonic()` 으로 잰다.** 주 근거(TXID·WAL 위치)의 존재 여부도 시계와 무관하다. 벽시계는 표시용(`checked_at`·`backlog_since`·`pending_since`·`db_changed_at`·`ltx_at`, ISO 8601 UTC 밀리초)으로만 쓴다. 벽시계로 재면 재조회 사이의 역행(1000 → 1060 → 900)이 확인된 backlog 를 정상으로 되돌렸다(review-1 재현). `Since` 는 (monotonic, 벽시계) 쌍이다.
- **전체 상태 = 별칭 중 가장 나쁜 것**(`unknown` > `backlog` > `caught_up`). `unknown` 은 "따라오는지 말할 수 없음"이라 뒤처진 것을 아는 `backlog` 보다 더 큰 문제(S3 장애·다른 기계의 쓰기·설정 오류)를 감출 수 있으므로 가장 나쁘게 본다. 별칭이 없으면 `unknown`.
- **부팅 상태 파일** `<db>.boot-state.json`: boot 가 exec 직전에 임시 파일 + rename 으로 원자적으로 쓴다(거부·실패한 부팅은 쓰지 않으므로 앞선 성공 부팅의 내용이 남는다). 헬스가 읽어 `boot_state` 로 보인다(아래 키만 옮긴다, 64 KiB 상한, 정규 파일만, 링크 따라가지 않음). 파일이 없거나 형식이 틀리면 `boot_state_error` 에 그 사실만 적고 상태는 바꾸지 않는다(boot 를 쓰지 않는 배포도 있다).
  ```json
  {
    "version": 1,
    "state": "fresh | match | adopt | unknown",
    "action": "proceed | restore | quarantine_and_restore | keep_local",
    "reason_code": "decide() 의 사유 코드 또는 orphan_sidecars",
    "reason": "사람용 한 줄",
    "unknown_at_boot": true,
    "litestream_version": "0.5.17",
    "at": "2026-10-08T01:02:03Z"
  }
  ```
  `unknown_at_boot` 는 `action` 이 `keep_local` 일 때만 참이다(`keep-local` 은 부팅 시 원격 조회가 성공했을 때만 통하므로 — D-12 — 이 표시는 원격이 비었거나 앞섰거나 로컬 메타가 없던 부팅을 뜻한다). `litestream_version` 은 읽지 못하면 `null`, `at` 은 UTC.
- **"마지막 업로드 시각"은 지연 지표로 쓰지 않는다.** 쓰기가 없는 정상 DB 도 업로드 시각은 오래되기 때문이다.

**갱신 방식**
- 요청마다 S3 를 부르지 않는다. 프로세스당 `Monitor` 하나의 데몬 스레드(`sqlite-ops-health`)가 `REFRESH` 초마다 별칭을 차례로 조회하고(`refresh_once()`), 뷰는 마지막 결과를 읽기만 한다. 요청은 `litestream` 을 기다리지 않는다.
- 스레드는 **첫 헬스 요청 때** 시작한다. `ready()` 에서 시작하지 않으므로 관리 명령·테스트·마이그레이션에서는 돌지 않는다. 그래서 첫 응답은 `unknown`(첫 조회 전)이다.
- 스레드 안의 예외는 삼키고 그 별칭을 `unknown`(사유)으로 기록한다. 스레드는 죽지 않는다.
- 포크 서버(gunicorn `--preload`)에서 스레드는 자식에 복제되지 않는다. 요청마다 PID 를 보고 바뀌었으면 결과·잠금을 버리고 스레드를 다시 시작한다. 모듈 잠금은 `os.register_at_fork` 로 자식에서 새로 만든다.
- 원격 조회는 별칭마다 `litestream version`(검증 버전 확인)과 `ltx` 두 번의 subprocess 다(타임아웃 10초·30초, §4-3). 조회가 매달리면 결과가 `REFRESH × 3` 보다 오래되어 `unknown` 이 된다.

**응답**: `{"version": 1, "status", "refresh", "backlog_grace", "databases": {alias: {status, code, reason, path, local_txid, remote_txid, checked_at, age, backlog_since, pending_since, db_changed_at, ltx_at, wal: {evidence, reason, ltx_wal_end, wal_commit_end}, boot_state, boot_state_error}}}`. 설정이 없거나 틀리면 `{version, status: "unknown", code, reason, databases: {}}`. TXID 는 Litestream 과 같은 16자리 16진수 문자열. `Cache-Control: no-store`.
- **HTTP 상태는 항상 200 이다.** 복제가 뒤처졌다고 로드밸런서가 앱을 빼면 안 된다(복제 지연은 데이터 손실 위험이지 요청 처리 불가가 아니다). 모니터링은 본문의 `status` 로 알람을 건다. `?strict=1` 이면 `caught_up` 이 아닐 때 503(HTTP 코드만 보는 모니터용, 로드밸런서에는 쓰지 않는다).
- **`ATOMIC_REQUESTS`**: Django 5.2.18·6.1.2 의 `BaseHandler.make_view_atomic()`(`django/core/handlers/base.py`)은 `ATOMIC_REQUESTS` 가 켜진 별칭마다 `alias not in view._non_atomic_requests` 이면 뷰를 `atomic(using=alias)` 로 감싸고, 그러면 뷰 실행 전에 연결이 열려 DB 파일이 생긴다(review-1 재현). `transaction.non_atomic_requests(using)` 은 별칭 하나만 넣으므로, `health_view._non_atomic_requests` 에 모든 별칭을 포함하는 집합(`__contains__` 가 항상 참인 `set` 하위 클래스)을 둔다. 사용자가 `non_atomic_requests` 를 덧씌워도 `add()` 가 그대로 동작한다.
- 인증하지 않는다. 응답에는 DB 경로·TXID·파일 시각·부팅 상태가 들어가므로 내부망에만 노출한다. **사유는 고정 문장에 경로·TXID·초만 넣고, subprocess stderr·예외 원문은 응답에 넣지 않는다.** Litestream 은 설정 오류 메시지에 endpoint 의 `user:password@` 를 그대로 찍는다(review-1 재현). 원문은 `logging.getLogger("django_sqlite_ops.health")` 에 WARNING 으로 남기며(별칭마다 같은 메시지는 한 번), 남기기 전에 `redact()` 한 곳의 규칙으로 가린다: URL userinfo, 쿼리의 `secret`·`password`·`token`·`signature`·`credential`·`access-key`·`auth` 류 키의 값, 같은 낱말이 든 `키: 값`·`키=값`. 부팅 상태 파일의 문자열도 같은 규칙을 거친다. 완전한 탐지기가 아니므로 원문을 응답으로 보내지 않는 것이 1차 방어다.
- 헬스는 "지금 복제가 따라오는가"만 말한다. "복구할 수 있는가"는 별도의 주기적 복원 검증(`sqlite_doctor --restore-test`, 2단계)으로 본다.
- D3 실측: 끊김 동안 Litestream 의 로그와 메트릭에 아무것도 보이지 않았다. 그래서 헬스는 Litestream 의 자기 보고에 기대지 않고, TXID 와 WAL 위치 비교로 직접 계산한다.
- 테스트(`tests/test_health.py`): 순수 함수 표(grace 경계, 원격 앞섬, 빈 목록, 메타 없음, `unknown_at_boot`, 오래된 결과, pending 경계·L0 변경 시 재시작), 가짜 조회 함수로 스레드(주기 갱신·예외 내구·PID 변경 재시작·첫 요청 전 미시작·벽시계 역행), 서브프로세스 Django 의 뷰(JSON 스키마, 200/strict 503, 연결이 열리지 않고 DB 파일이 생기지 않음 — 두 별칭 `ATOMIC_REQUESTS=True` 포함, 자격 증명이 응답·로그에 없음), 실제 Litestream `file://` 복제본(TXID: `replicate -once` 로 caught_up, 다른 복제본으로만 보내 grace 뒤 backlog, 빈 목록·조회 실패 unknown / 파일 시각: `-once` 뒤 커밋 → backlog, `replicate` 지속 실행 중 유휴 caught_up → 종료 → 쓰기 → grace 안 caught_up, 뒤 backlog). S3 끊김(L8)은 랩(#10) 몫이다. 라운드 3: 읽기 사이에 실제 체크포인트·헤더 변경·파일 교체를 끼우는 테스트(테스트에서 `os.pread` 를 감싼다. 운영 코드에 훅 없음) — TRUNCATE 는 `in_sync` 아님, 두 번 다 바뀌면 `changed during read`, 같은 내용으로 교체되면 재시도 뒤 `in_sync`, PENDING 은 유지, review-3 `checkpoint_race.py`(TRUNCATE·RESTART, 실제 Litestream·복원). 라운드 2: `wal.compare()` 를 실제 SQLite 가 쓴 `-wal` 바이트로(같은/다른 salt, 체크섬·salt 가 틀린 프레임, 쓰다 만 프레임, 근거 없음 사례, 링크·FIFO), L0 헤더 필드, 실제 0.5.17 L0 의 WAL 위치, review-2 `race.py`(8 × 1MiB, 3회 중 경쟁 재현 시 `db_not_replicated`, 어느 경우도 `in_sync` 아님)·`edge.py`, 바쁜 DB 의 TXID backlog 오탐.

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
- **검증 상태**: **재현함**(#8, channels-lite 0.4.0 · aiosqlite 0.22.1 · aiosqlitepool 1.0.0). 수신자 둘이 일반 채널 `work` 를 경쟁하고, 송신은 따로 한다. 수신자마다 다른 채널로 먼저 `send` 를 한 번 하면(풀이 그 연결을 다시 주므로 `total_changes > 0`) 메시지 하나가 두 수신자에게 모두 배달된다. 배치는 두 가지로 구분한다.
  - **한 프로세스의 독립 레이어·풀 둘**: 자식 인터프리터 하나·이벤트 루프 하나에서 레이어 인스턴스 둘(풀도 각자)을 함께 돈다. DB 경쟁은 실제지만 프로세스 간 스케줄링은 보지 않는다.
    - 두 SELECT 뒤에 배리어를 둔 결정적 시나리오(`pool_size=1`): 20/20 라운드 중복. 결함 존재와 수정 전 실패는 이 시나리오로 단언한다.
    - 계측 없이 공개 API `receive()` 두 개를 동시에 돈 시나리오(기본 `pool_size=10`): 중복은 두 SELECT 가 겹칠 때만 생기므로 횟수가 스케줄에 달려 있다(구현자 실험 20/20·50/50, 리뷰 고병렬 50회 중 5회는 19/20). 그래서 패치 전 횟수는 단언하지 않고 패치 후 중복 0·유실 0 만 단언한다.
  - **별도 프로세스 둘**: spawn 한 수신 프로세스 둘(각 `pool_size=1`), SELECT 뒤 프로세스 간 배리어, 송신은 부모 프로세스. 20/20 라운드 중복(리뷰 실험, 회귀 테스트로 옮김).
  - 대조군: 수신자 연결이 쓰기를 한 적이 없으면 배리어 시나리오 두 배치 모두 0/20(그래서 원인은 `total_changes` 다).
  - 패치 후 모든 시나리오 0/20. 테스트는 `tests/test_compat_channels_lite.py`.
- **함정(패치 범위 밖)**: 새 DB 에 여러 연결이 동시에 처음 `journal_mode=WAL` 로 바꾸면 그중 일부가 `database is locked` 로 실패할 수 있다(원인 미확정 — `busy_timeout` 을 먼저 걸어도 재현됨, 리뷰 대조 실험). channels-lite 는 연결마다 기본 `init_command` 로 WAL 을 실행하므로(0.4.0 `aio.py:50-58`) 새 채널 DB 에 여러 프로세스가 동시에 처음 붙을 때 생긴다(테스트 준비에서 재현). 배포 때(`migrate` 직후) 채널 DB 를 미리 `PRAGMA journal_mode=WAL` 로 바꿔 둔다. 코드로는 다루지 않는다(#8).
- **구현 순서**(지키기):
  1. **먼저 재현 테스트를 만든다.** 수신자 둘이 같은 일반 채널(`!` 없는 채널)을 경쟁하고, 같은 연결이 앞서 쓰기를 한 상태를 만든다. 패치 전에 중복 배달이 실패로 드러나야 한다. 재현이 안 되면 패치하지 않고 이 절을 "재현 안 됨"으로 갱신한다.
  2. 패치는 `cursor.rowcount == 1` 로 판정을 바꾸는 최소 교체다. 메서드 하나만 바꾼다.
  3. **버전 게이트**: 설치된 channels-lite 버전이 검증한 범위(`==0.4.0`)일 때만 적용한다. 범위 밖이면 적용하지 않고 `sqlite_doctor` 에 경고로 남긴다.
  4. **명시 적용**: 자동으로 적용하지 않는다. 설정 `SQLITE_OPS = {"PATCH_CHANNELS_LITE_AIO": True}` 이거나 `django_sqlite_ops.compat.channels_lite.apply()` 를 호출할 때만 적용한다. `apply()` 를 두 번 불러도 안전해야 한다(멱등).
  5. 패치 후 같은 재현 테스트가 통과해야 한다.
- ORM 판(`layers/core.py`)은 `aupdate()` 의 반환값(행 수)을 검사하므로 해당하지 않는다(0.4.0 `core.py:47-50`, 소스 고정 테스트 있음).
- **구현**(#8): `django_sqlite_ops/compat/channels_lite.py` 의 `apply()`·`status()`. 게이트는 `importlib.metadata.version("channels-lite") == "0.4.0"` 과 원본 메서드 `inspect.getsource()` 의 SHA-256 이 검증값과 같은지 둘 다 본다. 소스를 읽을 수 없으면 적용하지 않는다. 교체본은 `compat/_channels_lite_aio.py` 이고, 원본과의 차이가 두 줄(UPDATE 결과를 `cursor` 로 받기, 판정)뿐인지 테스트가 원본 소스와 diff 로 확인한다.

## 10. 회귀 랩 (실패 경계)

스파이크 랩(`docs/reference/compose.yaml`, `Dockerfile`, `entrypoint.sh`)을 바탕으로 `lab/` 에 다시 만든다. S3 대체는 SeaweedFS 다(MinIO 는 익명 pull 이 막혀 있었다). 장애 주입은 toxiproxy 로 한다.

구현은 `lab/`(설명 `lab/README.md`), 실행은 `scripts/lab.sh run [L1..L8|P1|P2|all]`·`bench`. 기본 `pytest`·CI 에는 들어가지 않는다(`RUN_LAB=1` 일 때만 수집). 결과: [`docs/research/lab-2026-10-08.md`](research/lab-2026-10-08.md).

| ID | 시나리오 | 기대 | 결과(2026-10-08) |
|---|---|---|---|
| L1 | 볼륨 없이 새 컨테이너 | `fresh` → 복원, 행 수 일치 | 통과(200/200, 무상태 교체 D1c 포함) |
| L2 | 옛 볼륨으로 재부팅(D4) | `unknown` → 거부(exit 2), 복제본 손상 0 | 통과(`remote_ahead`, 복제본 TXID·150건 그대로) |
| L3 | 로컬 메타만 삭제 | `unknown` → 거부 (스파이크는 여기서 새 DB 를 버렸다) | 통과(`no_local_meta`, DB 해시 그대로) |
| L4 | S3 끊김 중 부팅 | `unknown` → 거부 | 통과(`remote_error`. 연결 거부도 `ltx` 가 매달려 30초 타임아웃에서 거부) |
| L5 | 복제되지 않은 로컬 커밋이 있는 상태로 재부팅 | `match` → 진행, 커밋 보존 | 통과(65건 보존, 이후 복제) |
| L6 | `--on-unknown restore` 진행 중 kill → 재시작 | 반쯤 옮겨진 상태 없음, 재실행으로 완료 | 통과(rename 8곳 모두에서 kill — kill 시 rename 수 = k 확인, 재실행 완료, 템플릿의 DB·사이드카·메타 하위 8항목이 한 격리 디렉터리에 같은 해시, D-14 지점은 격리 2개) |
| L7 | 복원 실패(S3 객체 손상) | exit 4, 로컬 무변경 | 통과(새 컨테이너·격리 경로 모두 exit 4, 로컬 해시 그대로) |
| L8 | 헬스: S3 20초 끊김 | `backlog` → 복구 후 `caught_up` | 조건부: 업로드만 끊기면(L8b) `backlog` → `caught_up`. 조회까지 끊기면(L8a) 랩의 허용 시간 10초(가정: `REFRESH × 3` + 진행 중 조회 여유 `REFRESH` + 표본 간격 + 1초. 조회 시간에서 도출된 보장 상한은 아니다) 안에 `unknown`(`stale`)으로 바뀌어 유지 → `caught_up` — §7 표대로 원격 조회 실패는 `unknown` 이 먼저다 |

배포 프로필 종단 검증(P1·P2): 문서의 Dockerfile·compose·entrypoint·settings 를 그대로 띄워 첫 배포·`check --deploy`·`sqlite_doctor`·`docker stop`(uvicorn 워커 1·2: exit 0, 대표 실행 0.47·0.72초, 400/400 복제)·재부팅·빈 볼륨 소유자 복사를 확인했다.

- **PRAGMA 벤치**: §6-0 의 후보(`temp_store`, `mmap_size`, `cache_size`, `journal_size_limit`)를 켠 경우와 끈 경우의 처리량·p99·WAL 크기를 잰다. 차이가 재현될 때만 기본값으로 올린다. 2026-10-08 결과(반복 5): 넷 중 개선이 반복해서 재현된 것은 없고, `cache_size=-65536` 단독은 반복해서 나빠졌다. 기본값은 바꾸지 않는다([`lab-2026-10-08.md`](research/lab-2026-10-08.md#pragma-벤치)). 재측정(#26: `CONN_MAX_AGE` 0·None, WAL 이 자라는 쓰기 단계, 서버 시간·포화 곡선)에서도 재현된 차이는 없었다. 이 계측 조건에서 처리량이 포화했고 부하 발생기에는 여유가 있었다. 앱 CPU/GIL 병목이 의심되지만 원인은 미검증이다([`bench-2026-10-08.md`](research/bench-2026-10-08.md)).
- 랩 자원은 compose 프로젝트명(`dso-lab*`)과 라벨(`io.itda.dso-lab=1`)로 구분하고, 끝나면 반드시 `down -v` 한다(실패해도 `trap`). 같은 호스트에 다른 프로젝트 컨테이너가 있다.
- ASGI(uvicorn) 엔트리포인트로 돌린다. 스파이크는 WSGI(gunicorn)만 검증했다.
- L6 의 kill 지점은 `lab_hooks/sitecustomize.py` 가 rename 뒤마다 잠들어 맞춘다(패키지 코드는 그대로). L7 은 SeaweedFS filer API 로 LTX 객체 본문을 뒤집는다.

## 11. 테스트·검증 원칙
- `sqlite_database()` 출력은 프로필별 스냅샷 테스트로 고정한다. Django 를 막은 상태에서 import 되는지도 본다.
- 판정 함수(`decide.py`)는 모든 입력 조합을 표 기반 단위 테스트로 고정한다.
- Litestream CLI 출력 파싱은 실제 0.5.17 출력을 fixture 로 저장해 테스트한다.
- 결함 수정은 "수정 전에 실패하는 테스트"로 증명한다.
- "재현함 / 코드상 확인 / 미검증"을 구분해서 쓴다.

## 12. 미검증·확인 필요
- ASGI 에서의 VFS 별칭 `CONN_MAX_AGE` 동작 (미검증). 일반 별칭은 확인함: 동기 뷰에서 `None` 은 재사용되지 않고 fd 를 쌓는다([`bench-2026-10-08.md`](research/bench-2026-10-08.md#결과-연결-재사용))
- W003·W005 의 ASGI 판단은 설정만 본다. `PROFILE`·`ASGI_APPLICATION` 없이 ASGI 서버로 띄우는 경우는 정적으로 알 수 없어 미탐으로 남긴다(§6-1 'ASGI 판단'). W005 는 #26 원자료가 근거이고 랩에서 따로 재현하지 않았다. 양수 `CONN_MAX_AGE` 의 ASGI 동작은 재지 않았다. fd 한도를 올린 `None` 장시간 실행은 동시 16·30분만 쟀다([`fd-soak-2026-10-09.md`](research/fd-soak-2026-10-09.md#미검증)): 동시 수·요청 속도에 따른 정상 상태 수준, 닫히지 않은 연결을 무엇이 회수하는지는 미검증
- PRAGMA 후보의 5% 크기 효과: #26 실행은 반복 간 흔들림(약 7%)이 커서 검출하지 못했다. `cache_size` 단독 악화의 원인(#27)
- 실제 클라우드 S3·R2·Tigris (미검증. 비용이 들어 마스터 승인 필요)
- 회귀 랩은 SeaweedFS 4.48 + toxiproxy 2.12.0 으로만 돌았다(colima linux/arm64 와 GitHub Actions ubuntu-latest linux/amd64, 둘 다 11/11 통과 — [`lab-2026-10-08.md`](research/lab-2026-10-08.md#amd64-실행-github-actions-25)). 다른 S3 구현은 미검증
- S3 단절이 `ltx` 타임아웃(30초)보다 길 때 헬스가 `stale` 에서 `remote_error` 로 넘어가는지(L8a 는 20초라 보지 못함), 분 단위 단절에서 Litestream 의 로그 변화 (미검증)
- `single-server-multiproc` 의 채널 레이어 런타임(channels-nats 메시지 전달)을 컨테이너에서 (미검증. 랩 P2 는 설정 검사·워커 수까지)
- Windows 에서 boot CLI 의 파일 잠금과 디렉터리 rename 원자성 [확인 필요]. 그때까지 boot 는 `fcntl` 이 없으면 exit 64 로 거부한다
- channels-nats D2 의 196/200 이 하네스 문제인지 [확인 필요]
- "같은 이력" 증명 방법(자동 판정의 전제) [확인 필요]
- Litestream 상류 #1506(VFS 확장 로드), #1271·#1363(writable VFS) 머지 여부
