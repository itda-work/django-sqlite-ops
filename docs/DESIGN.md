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

1. **잠금 획득**: `<db>.boot.lock` 에 `flock(LOCK_EX | LOCK_NB)`. 이미 잡혀 있으면 exit 5. 잠금 fd 는 **exec 된 명령에 상속된다**. 그래서 `litestream replicate -exec ...` 와 그 자식이 살아 있는 동안 같은 볼륨에서 두 번째 boot 는 잠금에서 막힌다(exit 5). 거부·실패로 돌아올 때는 잠금을 풀고 끝난다. 다른 머신 사이의 이중 replicate 는 막지 못한다. 이는 문서와 `sqlite_doctor` 경고로 다룬다. `fcntl` 이 없는 플랫폼(Windows)은 판정할 수 없으므로 exit 64 다(§12).
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
- 격리·설치 도중의 파일 오류는 exit 4 다. 격리가 반쯤이면 `.partial` 이 남고 다음 부팅이 2 단계에서 끝낸다.

### 4-3. 판정 (상태 4가지)
| 상태 | 조건 | 기본 동작 |
|---|---|---|
| `fresh` | 로컬 DB 파일과 로컬 메타가 모두 없고, 원격 조회 성공. 원격이 빈 목록이면 `--init-new` 도 있어야 한다(D-13) | 원격에 복제본이 있으면 복원, 빈 목록(+`--init-new`)이면 그대로 진행(새 DB) |
| `match` | 로컬 메타의 최대 TXID ≥ 원격 최대 TXID, 그리고 원격 조회 성공 | 그대로 진행 |
| `adopt` | `--adopt-existing` 이 있고, 로컬 DB 있음 + 로컬 메타 없음 + 원격 조회 성공·빈 목록 (D-11) | 그대로 진행(기존 DB 를 처음 Litestream 에 올림) |
| `unknown` | 그 밖의 모든 경우: 원격 조회 실패·타임아웃·파싱 실패, 로컬 메타 없음, DB 없이 메타만 남음, 로컬 DB 는 있는데 원격이 빈 목록, 로컬 DB 도 원격도 없는데 `--init-new` 없음, 원격이 앞섬 | **기동 거부(exit 2)**. 사유를 한 줄로 출력 |

- `--on-unknown restore` : 복원을 임시 디렉터리에 먼저 끝낸 뒤, 로컬을 `<db>.stale-<ts>/` 디렉터리 하나로 옮기고 복원본을 설치한다. 여러 파일을 한 번에 옮기는 원자적 연산은 없으므로 **재개 가능하게** 옮긴다(L6).
  1. `<db>.stale-<ts>.partial/` 을 만들고, 그 안에 `manifest.json`(옮길 원래 경로와 이름, 순서)을 임시 파일 + rename 으로 쓴다. 디렉터리를 fsync 한다.
  2. 대상을 순서대로 하나씩 rename 해 넣는다. 순서는 `boot/cli.py` 의 `quarantine_targets()` 한 곳에 있다: DB → `-wal` → `-shm` → `-journal` → 메타 디렉터리. 없는 대상은 건너뛴다.
  3. fsync(디렉터리) 뒤 `.partial` 을 `<db>.stale-<ts>/` 로 rename 한다. 이 rename 이 격리의 완료 표시다.
  - 도중에 죽으면 `.partial` 이 남는다. 다음 부팅은 판정 전에 manifest 를 따라 아직 원래 자리에 있는 대상만 마저 옮기고 3 을 한다(멱등). manifest 를 따르므로 재개 때 `--meta-path` 가 달라도 원래 목록대로 옮긴다. manifest 를 쓰기 전에 죽었으면 옮긴 파일이 없으므로 `.partial` 만 지운다. 원래 자리와 `.partial` 안에 같은 이름이 둘 다 있으면(그 사이 누가 새 파일을 만들었다) 판정할 수 없으므로 exit 2.
  - 격리를 마친 뒤 설치 전에 죽으면 로컬 DB·메타가 없으므로 다음 부팅은 `fresh` 로 복원한다. 설치 뒤 exec 전에 죽으면 D-14 와 같은 상태(DB 있음, 메타 없음)가 되어 `--on-unknown restore` 면 한 번 더 격리·복원한다. 어느 kill 지점에서도 DB 와 메타가 다른 격리 디렉터리로 갈라지지 않는다(테스트로 고정).
  - rename 은 같은 파일시스템 안에서만 원자적이다. 메타 디렉터리가 DB 와 다른 파일시스템에 있으면 복원 전에 거부한다(exit 2).
- `--on-unknown keep-local` : 로컬을 그대로 두고 진행하되, stderr 와 헬스 상태에 `unknown_at_boot` 를 남긴다. **원격 조회가 성공했을 때만** 통한다. 원격 조회 실패면 정책과 무관하게 거부한다(D-12, S3 장애 중 옛 볼륨 재부팅 방지).
- `--adopt-existing` : 기존 DB 를 처음 Litestream 에 올릴 때 쓴다. 정확히 (로컬 DB 있음, 로컬 메타 없음, 원격 조회 성공·빈 목록) 일 때만 `adopt` 로 진행하고, 그 밖의 모든 조합에서는 결과가 바뀌지 않는다. 그래서 켜 둔 채로 두어도 다른 사고를 통과시키지 않는다. `keep-local` 을 최초 도입 절차로 쓰지 않는다(D-11). **도입 배포가 끝나면 끈다.** 판정 함수는 '도입했음'을 기억하지 않으므로, 켜 둔 채로 두면 나중에 메타와 복제본이 함께 사라진(예: prefix 오타) 상황에서 같은 조합이 다시 성립해 그 DB 를 새 복제본으로 올린다(D-13 과 같은 이유).
- `--init-new` : 생애 첫 배포에서 새 DB 로 시작할 때 쓴다. 정확히 (로컬 DB 없음, 로컬 메타 없음, 원격 조회 성공·빈 목록) 일 때만 `fresh/new_db` 로 진행하고, 없으면 같은 조합을 `unknown/no_replica_no_local` 로 거부한다. Litestream 은 복제본 경로·prefix 오타와 "복제본 없음"을 같은 빈 목록(rc 0, `[]`)으로 돌려주므로(#3 실측) 빈 목록만으로 새 DB 를 시작하면 오타 난 경로에 새 DB 를 복제해 기존 복제본을 버리게 된다. 이 조합 밖에서는 결과를 바꾸지 않지만, 판정 함수는 '첫 배포였음'을 기억하지 않으므로 나중에 DB·메타가 함께 사라지고 복제본 경로까지 틀리면 같은 조합이 다시 성립한다. 그래서 **첫 배포 뒤에는 끈다**(D-13 정정).
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
| `2` | 거부: `unknown`(+ `refuse` 정책, 원격 조회 실패 등), DB 없이 사이드카만 남음, `.partial` 이 둘 이상, DB 경로가 정규 파일이 아님, 설치 자리가 비어 있지 않음, 상태 파일을 쓸 수 없음 |
| `3` | 무결성 실패: `PRAGMA quick_check` 가 `ok` 가 아니거나 DB 를 열 수 없음 |
| `4` | 복원 실패: `litestream restore` 실패·타임아웃·결과 이상, 격리·설치 도중의 파일 오류. 복원 실패면 로컬은 바뀌지 않았다 |
| `5` | 잠금 실패: 다른 boot 나 그것이 exec 한 명령이 잠금을 쥐고 있음, 잠금 파일을 만들 수 없음 |
| `64` | 사용법 오류: 인자 오류, `--` 뒤 명령 없음, POSIX `fcntl` 이 없는 플랫폼 |
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
  | `temp_store`, `mmap_size`, `cache_size`, `journal_size_limit` | 후보 | **미검증.** 랩 벤치(§10) 뒤에 기본값으로 올릴지 정한다 | 끔 |
  | `wal_autocheckpoint` | 건드리지 않음 | Litestream 이 체크포인트를 관리한다. 바꾸면 복제와 충돌할 수 있다 [확인 필요] | 끔 |
  | `CONN_MAX_AGE` | 프로필별 | VFS 별칭은 `None` 필수(실측 1,008ms → 1.7ms, WSGI). ASGI 는 미검증 | 2단계(VFS 프로필) |

- `init_command` 는 연결마다 실행된다. 그래서 가벼운 PRAGMA 만 넣고, 데이터나 스키마를 바꾸는 문장은 넣지 않는다.


### 6-1. 정적 체크 (`manage.py check`, DB 를 열지 않음)
기준값은 §6-0 의 표에서 읽는다. `sqlite_database()` 를 쓰지 않은 설정(직접 쓴 dict, dj-lite 결과)도 같은 기준으로 검사한다.

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
- 별칭마다 실제 `journal_mode`, `busy_timeout`, `synchronous`, SQLite 버전, 파일 크기, WAL 크기를 보고, §6-0 권장값과 다른 항목을 표시한다. `init_command` 가 실행된 뒤의 **실제 값**을 보므로 정적 체크와 결과가 다를 수 있다.
- 마운트 종류를 본다. 네트워크 파일시스템(NFS·SMB)이면 경고한다. 판정할 수 없으면 `unknown` 으로 적는다.
- Litestream 설정 파일을 읽고, 그 안의 DB 경로가 `DATABASES` 와 맞는지 대조한다.
- 채널 레이어 백엔드와 그 의미론 요약을 출력한다(§8 표 참조).
- 출력은 사람용 텍스트와 `--json` 두 가지다. 종료 코드는 0(문제 없음), 1(경고), 2(오류)다.

## 7. 헬스

- `caught_up`: 원격 최대 TXID == 로컬 최대 TXID
- `backlog`: 로컬 TXID 가 원격보다 앞서 있고, 그 상태가 `SQLITE_OPS_BACKLOG_GRACE`(기본 60초)보다 오래 지속됨
- `unknown`: 원격 조회 실패, 메타를 읽을 수 없음, 부팅 때 `keep-local` 로 진행함(부팅 상태 파일의 `unknown_at_boot`)(`keep-local` 은 부팅 시 원격 조회가 성공했을 때만 통하므로 — D-12 — 이 표시는 원격이 비었거나 앞섰거나 로컬 메타가 없던 부팅을 뜻한다)
- **부팅 상태 파일** `<db>.boot-state.json`: boot 가 exec 직전에 임시 파일 + rename 으로 원자적으로 쓴다(거부·실패한 부팅은 쓰지 않으므로 앞선 성공 부팅의 내용이 남는다). 헬스(#7)가 읽는다.
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
  `unknown_at_boot` 는 `action` 이 `keep_local` 일 때만 참이다. `litestream_version` 은 읽지 못하면 `null`, `at` 은 UTC.
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

- **PRAGMA 벤치**: §6-0 의 후보(`temp_store`, `mmap_size`, `cache_size`, `journal_size_limit`)를 켠 경우와 끈 경우의 처리량·p99·WAL 크기를 잰다. 차이가 재현될 때만 기본값으로 올린다.
- 랩 자원은 compose 프로젝트명과 라벨로 구분하고, 끝나면 반드시 `down -v` 한다. 같은 호스트에 다른 프로젝트 컨테이너가 있다.
- ASGI(uvicorn) 엔트리포인트로 돌린다. 스파이크는 WSGI(gunicorn)만 검증했다.

## 11. 테스트·검증 원칙
- `sqlite_database()` 출력은 프로필별 스냅샷 테스트로 고정한다. Django 를 막은 상태에서 import 되는지도 본다.
- 판정 함수(`decide.py`)는 모든 입력 조합을 표 기반 단위 테스트로 고정한다.
- Litestream CLI 출력 파싱은 실제 0.5.17 출력을 fixture 로 저장해 테스트한다.
- 결함 수정은 "수정 전에 실패하는 테스트"로 증명한다.
- "재현함 / 코드상 확인 / 미검증"을 구분해서 쓴다.

## 12. 미검증·확인 필요
- ASGI 에서의 `CONN_MAX_AGE` 와 VFS 동작 (미검증)
- 실제 클라우드 S3·R2·Tigris (미검증. 비용이 들어 마스터 승인 필요)
- Windows 에서 boot CLI 의 파일 잠금과 디렉터리 rename 원자성 [확인 필요]. 그때까지 boot 는 `fcntl` 이 없으면 exit 64 로 거부한다
- channels-nats D2 의 196/200 이 하네스 문제인지 [확인 필요]
- "같은 이력" 증명 방법(자동 판정의 전제) [확인 필요]
- Litestream 상류 #1506(VFS 확장 로드), #1271·#1363(writable VFS) 머지 여부
