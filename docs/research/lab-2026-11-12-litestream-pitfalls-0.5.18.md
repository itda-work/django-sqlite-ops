# Litestream 함정 재측정 — 0.5.17 대 0.5.18 (2026-11-12, #105)

CLAUDE.md '함정'·README·DESIGN 에 적힌 Litestream 동작은 0.5.17 에서 잰 것이다. 0.5.18(2026-11-03)을 버전 게이트에 넣은 뒤(#99) 그 동작이 여전히 맞는지 두 판으로 다시 쟀다. #99 에서 이미 잰 두 항목(S3 연결 거부 때 `ltx` 매달림, 단절 중 로그)은 다시 재지 않았다([#99 기록](lab-2026-11-12-litestream-0.5.18.md)).

결론: 아홉 항목 가운데 **바뀐 것은 둘**이다.

- **스킴 없는 endpoint**: 0.5.17 은 끝없이 기다리고, 0.5.18 은 약 7초(restore 는 약 14초) 뒤 rc 1 로 끝난다.
- **VFS 확장 로드(Linux)**: 0.5.17 은 로드 뒤 일반 connect 가 실패하고, 0.5.18 은 실패하지 않는다(상류 #1506).

boot 의 판정은 두 판 모두 그대로 맞았다(스킴 없는 endpoint 는 두 판 모두 exit 2 `remote_error`). 패키지 코드는 바꾸지 않았다.

## 환경

- 호스트: macOS 26(arm64). 0.5.17 은 brew 바이너리(`/opt/homebrew/bin/litestream`), 0.5.18 은 #99 에서 받은 릴리스 `litestream-0.5.18-darwin-arm64`(같은 릴리스 `checksums.txt` 와 SHA-256 일치)를 썼다.
- 항목 8·9 는 colima(linux/arm64, Docker 29.5.2) 컨테이너에서 쟀다. 컨테이너에는 모두 `io.itda.dso-lab=1` 라벨을 붙였고, 끝난 뒤 `docker ps -a`·`docker volume ls`·`docker network ls`(라벨 필터)가 모두 비었다.
- 측정 스크립트는 `.work/issue-105/`(git 무시)에 두고 썼다. 절차는 아래 항목마다 적었고, 로컬에서 재현되는 항목은 실제 바이너리 테스트 `tests/test_litestream_pitfalls.py` 로 옮겼다. CI `litestream` 잡(판 2 × Python 2)이 검증한 판마다 돌린다.

## 요약

| # | 문서의 주장(0.5.17 에서 잰 것) | 0.5.17 | 0.5.18 | 판정 | 고정한 곳 |
|---|---|---|---|---|---|
| 1 | endpoint 에 `http://` 가 빠지면 restore·`ltx` 가 무한 대기한다 | 60초 상한까지 끝나지 않음 | `ltx` 7.1초·`restore` 14.1초 뒤 rc 1 | **바뀜** | `test_no_scheme_endpoint_by_version`·`test_no_scheme_endpoint_stderr_names_the_cause`·`test_http_scheme_endpoint_reaches_plain_server` |
| 2 | 연결은 받고 응답하지 않는 서버에 `ltx` 가 매달린다 | 매달림 | 매달림 | 그대로 | 기존 `test_real_hanging_s3_endpoint_times_out`(스킴 있음·없음) |
| 3 | `restore -if-db-not-exists` 는 0바이트 DB 가 있으면 rc 0 으로 건너뛴다 | rc 0, 0바이트 그대로 | 같음 | 그대로 | `test_if_db_not_exists_skips_zero_byte_file` |
| 4 | `restore -o` 는 0바이트 파일은 덮고 내용 있는 파일은 거부한다 | 덮음 / rc 1 거부 | 같음 | 그대로 | `test_restore_output_zero_byte_is_overwritten_nonempty_refused` |
| 5 | Litestream 을 붙이면 rollback journal DB 가 WAL 로 바뀐다 | `delete` → `wal` | 같음 | 그대로 | `test_replicate_switches_rollback_journal_db_to_wal` |
| 6 | restore 로 만든 DB 옆에는 로컬 메타가 없다 | `app.db` 하나 | 같음 | 그대로 | `test_restored_db_has_no_local_meta` |
| 7 | `ltx DB_PATH` 는 로컬 메타가 아니라 복제본을 나열한다 | 메타에 LTX 가 있어도 `[]` | 같음 | 그대로 | `test_ltx_lists_replica_not_local_meta` |
| 8 | Linux 에서 litestream-vfs 를 로드하면 그 뒤 일반 connect 가 실패한다 | 실패(`automatic extension loading failed`) | **실패하지 않음** | **바뀜** | 이 기록(§8) |
| 9 | S3 가 끊겨도 `status`·메트릭에 아무것도 남지 않는다 | `status` rc 0·`ok`, 오류 메트릭 0 | 같음 | 그대로 | 이 기록(§9) |

근거 등급은 모두 '재현함'이다(항목 8·9 는 컨테이너에서 판마다 1–2회).

## 1. endpoint 에 스킴이 없을 때

**절차**: 파이썬 표준 라이브러리 `http.server` 로 평문 HTTP 서버를 `127.0.0.1` 의 임의 포트에 띄운다(GET 에 200). 복제본 설정의 `endpoint` 를 `127.0.0.1:<포트>`(스킴 없음)로 두고 `litestream ltx -config … -level all -json <db>` 와 `litestream restore -config … -json -o <새 경로> <db>` 를 각각 60초 상한으로 부른다. 대조로 `http://127.0.0.1:<포트>` 를 쓴다.

| 판 | 명령 | rc | 소요 | 평문 서버가 받은 연결 | stderr 마지막 줄 |
|---|---|---|---|---|---|
| 0.5.17 | `ltx` | — (60초 상한에서 끊음) | 60.0초 | 7 | 없음 |
| 0.5.17 | `restore` | — (60초 상한에서 끊음) | 60.0초 | 6 | 없음 |
| 0.5.18 | `ltx` | 1 | 7.1초 | 8 | `Error: operation error S3: ListObjectsV2, exceeded maximum number of attempts, 8, https response error StatusCode: 0, RequestID: , HostID: , request send failed, Get "https://127.0.0.1:<포트>/lab?list-type=2&prefix=app%2F0000%2F": http: server gave HTTP response to HTTPS client` |
| 0.5.18 | `restore` | 1 | 14.1초 | 16 | `Error: get v0.3.x time bounds: s3: list generations: operation error S3: ListObjectsV2, exceeded maximum number of attempts, 8, … http: server gave HTTP response to HTTPS client` |
| 두 판 | `http://` 붙인 `ltx` | 0 (`[]`) | 0.03초 | 평문 GET 10건 | 없음 |

- 두 판 모두 HTTPS 로 접속했다: 평문 서버가 TLS ClientHello 바이트(`\x16\x03\x01…`)를 요청 줄로 읽고 400 을 냈다.
- 0.5.18 의 `restore` 가 `ltx` 의 두 배인 것은 v0.3.x 세대 목록 조회(재시도 8회)를 한 번 더 하기 때문으로 보인다(stderr 의 `get v0.3.x time bounds` — 원인은 미검증).
- 원인이 상류 #1528(LTX 스트림 읽기 시간 제한)인지 AWS SDK 재시도 동작의 변화인지는 미검증이다. 동작은 재현했다.

**boot 에서 보이는 것**(같은 서버, 로컬 DB 없음, `python -m django_sqlite_ops.boot --db … --config … --litestream <바이너리> -- true`):

| 판 | exit | 소요 | 사유 |
|---|---|---|---|
| 0.5.17 | 2 `remote_error` | 30.1초(`--ltx-timeout` 기본 30) | `remote lookup failed: … ltx timed out after 30s` |
| 0.5.18 | 2 `remote_error` | 7.1초 | `remote lookup failed: litestream ltx exited with 1: Error: operation error S3: ListObjectsV2, exceeded maximum number of attempts, 8, … Get "https://127.0.0.1:<포트>/lab?list-type=2&prefix=app%2F0000%2F": http: server gave HTTP response to HT...` |

두 판 모두 `hint: fix replica access (endpoint needs http:// for plain HTTP, credentials, network)` 가 나온다. 0.5.18 의 사유는 300자에서 잘린다(`boot/litestream.py` 의 한 줄 요약). 끝의 `HTTP response to HTTPS client` 는 endpoint·버킷·prefix 길이에 따라 잘리지만, 앞부분의 `Get "https://…` 는 남는다. 이 동작은 판정을 바꾸지 않으므로 코드는 그대로 두고 문서(README 문제 해결)만 판별로 고쳤다.

## 2. 응답하지 않는 서버

기존 테스트 `test_real_hanging_s3_endpoint_times_out`(연결은 받고 아무것도 보내지 않는 `SilentServer`, 스킴 있음·없음 두 경우)이 두 판에서 통과했다(아래 'pytest'). `--ltx-timeout` 3초에서 `timed out after 3s` 로 끊긴다. 양방향 무응답 S3 에 0.5.18 도 매달린다는 #99 L8d·L4 의 결과와 같다.

## 3·4·6. restore 와 기존 파일·로컬 메타

**절차**: `file` 복제본 설정으로 `replicate` 를 띄워 WAL DB 에 행 5개를 쓰고 멈춘다. 그 복제본에서:

| 항목 | 명령 | 두 판의 결과 |
|---|---|---|
| 3 | 0바이트 파일 `zero/app.db` 를 두고 `restore -if-db-not-exists -o zero/app.db <db>` | rc 0, stdout `level=INFO msg="database already exists, skipping"`, 파일은 0바이트 그대로 |
| 3(대조) | 파일 없이 같은 명령 | rc 0, 16384바이트 복원 |
| 4 | 0바이트 파일에 `restore -o` | rc 0, 덮어써 16384바이트(행 5개) |
| 4 | 100바이트 파일에 `restore -o` | rc 1, `Error: cannot restore, output path already exists and is not empty: … Use -force to overwrite`, 파일 그대로 |
| 6 | 빈 디렉터리에 `restore -o restored/app.db` | rc 0, 디렉터리에 `app.db` 하나(`.app.db-litestream/` 없음) |

## 5. WAL 전환

**절차**: 표준 `sqlite3` 로 rollback journal(`delete`) DB 를 만들고 `file` 복제본으로 `replicate` 를 띄운 뒤 0.5초마다 새 연결로 `PRAGMA journal_mode` 를 읽는다.

두 판 모두 `delete`, `delete`, 그다음부터 `wal`(약 1초 안)이었고, `replicate` 를 멈춘 뒤에도 `wal` 이었다. 랩에서 Django 기본 설정이 WAL 로 돈 #83 의 관측과 같다.

## 7. `ltx DB_PATH` 와 로컬 메타

**절차**: 복제한 DB 와 그 로컬 메타(`ltx/0/0000000000000002-0000000000000002.ltx`)는 두고 복제본 디렉터리만 옆으로 치운 뒤 `ltx -level all -json <db>` 를 부른다.

두 판 모두 rc 0, `[]` 다. 로컬 메타의 LTX 는 나열하지 않는다(DESIGN §4-3 '로컬 TXID' 의 근거 그대로).

## 8. litestream-vfs 확장 로드 (Linux)

**절차**: `python:3.13-slim@sha256:e48f59cd…`(Python 3.13.18, SQLite 3.46.1, aarch64) 일회용 컨테이너에서 PyPI `litestream-vfs` wheel(`manylinux_2_35_aarch64`)을 `pip install --require-hashes` 로 설치한다. 해시는 PyPI JSON API 의 SHA-256 이다.

- 0.5.17: `5ec86065b155c4fe5f85ceaf1aa07534018b6bc5e02a07c2b6f138f3567230ca`
- 0.5.18: `bf83a8daef9f28e78f8c4f58326a6c28e143672cb277d56a422cc3fefb696c21`

그다음 [`docs/reference/vfs_workaround_spike.py`](../reference/vfs_workaround_spike.py) 의 로드 절차 그대로 `:memory:` 연결에 `load_extension(loadable_path(), entrypoint="sqlite3_litestreamvfs_init")` 를 하고, 이어서 일반 연결(`:memory:` 와 파일 DB)을 열어 테이블을 만들고 쓴다. 우회(`sqlite3_reset_auto_extension()`) 없이 한 번, 우회를 넣고 한 번, 각각 새 프로세스로 돌렸다.

| 판 | 우회 | 확장 로드 | 일반 `:memory:` | 일반 파일 DB |
|---|---|---|---|---|
| 0.5.17 | 없음 | ok | `DatabaseError: automatic extension loading failed: ` | 같은 오류 |
| 0.5.17 | 있음 | ok | ok | ok |
| 0.5.18 | 없음 | ok | **ok** | **ok** |
| 0.5.18 | 있음 | ok | ok | ok |

- 0.5.17 에서 실패가 재현돼 절차가 맞음을 먼저 확인했다. 0.5.18 은 우회 없이도 일반 connect 가 된다. 상류 #1506 이 0.5.18 에 들어갔다는 릴리스 노트와 맞는다.
- 두 판 모두 로드 뒤 `file:…?vfs=litestream&mode=ro` URI 는 `no such vfs` 가 아니라 `SQL logic error` 였다(VFS 는 등록됐고, 복제본 설정 없이 열었기 때문의 오류로 보인다 — VFS 자체의 동작은 이 측정의 범위 밖).
- amd64 는 재지 않았다(미검증).

## 9. S3 단절 중 `litestream status` 와 메트릭

**절차**: `scripts/lab.sh up`(판은 `LAB_LITESTREAM_VERSION`)으로 SeaweedFS·toxiproxy 를 띄운다. 랩 앱 이미지(`dso-lab-app:<sha>-ls<판>`)의 일회용 컨테이너를 랩 네트워크에 붙여, 랩의 `litestream.yml` 대신 측정용 설정을 `/tmp` 에 만든다. 설정은 랩과 같은 S3(`http://toxiproxy:18333`, 버킷 `dso-lab`, 새 prefix)에 `addr: ":9090"` 만 더했다([공식 설정 문서](https://litestream.io/reference/config/)의 메트릭 항목). 1초에 한 번 쓰면서 다음 구간을 거친다. 5초마다 `litestream status -json` 과 `http://127.0.0.1:9090/metrics` 를 읽는다.

before 20초 → refuse(toxiproxy 프록시 `s3` 끔) 40초 → recover 15초 → hang(`timeout` toxic, `timeout` 0, downstream·upstream 둘 다) 40초 → after 20초.

**`status`**: 두 판 모두 모든 구간에서 rc 0, `status` 는 첫 표본만 `not initialized` 이고 나머지는 모두 `ok` 였다. `local_txid`·`wal_size` 는 단절 중에도 늘었다. `status` 는 로컬만 본다는 공식 문서([status](https://litestream.io/reference/status/))와 맞다. 단절은 `status` 에 나타나지 않는다.

**메트릭**: 두 판 모두 같은 family 14개가 나왔다.

`litestream_checkpoint_count`·`checkpoint_seconds`·`compaction_verify_error_count`·`db_size`·`disk_full`·`l0_retention_files_total`·`replica_operation_bytes`·`replica_operation_total`·`sync_count`·`sync_error_count`·`sync_seconds`·`total_wal_bytes`·`txid`·`wal_size`.

| 메트릭 | 0.5.17 | 0.5.18 |
|---|---|---|
| `litestream_sync_error_count` | 모든 구간 0 | 모든 구간 0 |
| `litestream_compaction_verify_error_count` | 모든 구간 0 | 모든 구간 0 |
| `litestream_replica_operation_total{operation="PUT"}` | before 14 → refuse 20 → 이후 **끝까지 20** | before 16 → refuse 20(멈춤) → recover 74 → hang 78(멈춤) → after 136 |
| `litestream_sync_count`·`litestream_txid` | 단절 중에도 증가 | 단절 중에도 증가 |
| 상류 #1535 의 `litestream_validation_checks_total`·`litestream_last_validation_success*` | 없음 | **없음**(기본 설정. 검증 주기를 켜는 설정은 재지 않았다 — 미검증) |
| 단절 중 Litestream WARN/ERROR 로그 | 0줄 | refuse 구간에 3줄(`monitor error` 1, `l0 retention enforcement failed` 2), hang 구간 0줄 |

- 오류를 직접 말하는 메트릭(`sync_error_count`)은 두 판 모두 단절에 반응하지 않았다. `sync` 는 로컬 WAL → LTX 단계라 S3 와 무관하게 성공한다.
- 단절을 간접으로 보여 주는 것은 `replica_operation_total{operation="PUT"}` 이 늘지 않는 것뿐이다. 그러나 쓰기가 없을 때도 늘지 않으므로 그것만으로는 단절과 구분되지 않는다.
- 0.5.17 은 이 실행에서 refuse 이후 끝까지(recover 15초·after 20초 포함) PUT 이 다시 늘지 않았다. #99 L8c 의 0.5.17 복구(7.1초)와 다르다. 이 측정은 한 번뿐이고 구간이 짧아 원인(재시도 백오프가 hang 구간과 겹쳤는지 등)은 미검증이다. 헬스는 TXID 비교로 이 상태를 `backlog`/`unknown` 으로 본다.
- 0.5.18 의 로그 줄은 #99 L8c 에서 본 것과 같은 종류다(연결 거부에만 나오고 무응답에는 0줄).

헬스가 메트릭이나 로그를 쓰도록 바꾸는 것은 이 이슈의 범위 밖이다(#105 본문). 관측으로는 TXID 를 직접 비교하는 지금 방식을 바꿀 근거가 없다.

## pytest

| PATH 의 litestream | 명령 | 결과 |
|---|---|---|
| 0.5.17 | `uv run pytest -q` | 3823 passed, 3 skipped |
| 0.5.18 | `REQUIRE_LITESTREAM=1 uv run pytest -q -rs` | 3824 passed, 2 skipped |

skip 2개는 macOS 의 대소문자 무시 볼륨 전용 테스트다. 0.5.17 의 skip 하나 더는 `test_no_scheme_endpoint_stderr_names_the_cause` 다(끝나지 않는 판이라 stderr 를 볼 수 없다. 그 판의 동작은 `test_no_scheme_endpoint_by_version` 이 본다).

`tests/test_litestream_pitfalls.py` 만 돌리면 0.5.17 에서 약 44초(스킴 없는 endpoint 테스트가 타임아웃 25·40초를 기다린다), 0.5.18 에서 약 26초다.
