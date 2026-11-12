# 앱 쪽 `wal_autocheckpoint` 와 Litestream, WAL·원격 L0 증가 (2026-11-12, #106)

DESIGN §6-0 의 `wal_autocheckpoint` 행은 '바꾸면 복제와 충돌할 수 있다 [확인 필요]' 였다. 상류 #1540(외부 체크포인트로 WAL 이 잘린 뒤 복제본에서 커밋이 빠지는데 `integrity_check` 는 통과)이 이 패키지 구성에서 일어나는지, Litestream 이 멈췄을 때 WAL 이 어떻게 자라는지, 컴팩션이 멈추면 헬스가 무엇을 말하는지를 랩에서 쟀다. 패키지 코드는 바꾸지 않았다.

## 결론

조건(아래 '환경'·'부하')을 결론 옆에 둔다. '빠지지 않았다'는 **이 조건에서 재현되지 않았다**는 뜻이다.

1. **Q1 — 빠진 커밋은 재현되지 않았다.** Litestream 0.5.17·0.5.18 × `wal_autocheckpoint` 1000·0 × {프로필 그대로 정상 운용, 부하 중 Litestream 만 SIGTERM → 20초 뒤 재기동(연결을 닫는 앱 / 연결을 쥔 앱)} × 3회(36회), 모두 앱이 커밋한 행(27,020개, 앱이 도중에 끝난 1회는 15,330개)이 복원본에 그대로 있었고 `integrity_check` 는 `ok` 였다(헬스 `caught_up` 뒤, `docker stop` 뒤 둘 다). Litestream 이 없던 20초 동안 WAL 은 13~37번 되감겼다 — 상류 #1540 코멘트(Litestream 정지 중 앱의 자동 체크포인트가 WAL 을 여러 번 재시작)와 같은 꼴이지만 이 부하·시간에서는 행이 빠지지 않았다(재현함: 빠지지 않음).
2. **Q2 — Litestream 이 살아 있으면 WAL 을 되감는 쪽은 Litestream 이다.** 프로필 그대로 90초 부하에서 WAL 헤더 salt 변화(되감김의 하한)는 앱 `wal_autocheckpoint` 1000 이든 0 이든 42~44회로 같았다. Litestream 의 읽기 잠금은 앱의 체크포인트를 막는다: Litestream 을 SIGSTOP 으로 얼린 동안(읽기 잠금을 쥔 채) 앱이 1000 이어도 WAL 은 한 번도 되감기지 않고 535MB 까지 자랐다. 같은 1000 에서 Litestream 프로세스가 없으면(연결 유지) WAL 은 6~7MB 에 머물렀다(앱의 자동 체크포인트가 되감음). Litestream INFO 로그에는 체크포인트 줄이 없다(0.5.18 만 500MB 를 넘긴 WAL 에서 `forcing truncate checkpoint`·`wal restarted by passive checkpoint` 한 줄).
3. **Q3 — WAL 이 자라는지는 `wal_autocheckpoint` 보다 'Litestream 이 어떻게 멈췄나'와 `CONN_MAX_AGE` 가 정한다.** 쓰기 약 1MB/s(행 데이터)에서:
   - Litestream 이 **얼었을 때**(읽기 잠금을 쥔 채, 프로필 그대로 가능): ckpt 1000·0 모두 약 178 MB/분. 180초에 535MB. 쓰기·읽기 p95 는 거의 그대로(쓰기 8→11ms). 헬스는 `backlog`(`db_not_replicated`).
   - Litestream 이 **없고** 앱이 요청마다 연결을 닫을 때(`CONN_MAX_AGE=0`, 프로필 기본): ckpt 와 무관하게 자라지 않는다(최대 0.4~5.3MB). 마지막 연결이 닫힐 때 SQLite 가 체크포인트하고 `-wal` 을 지우기 때문이다(표본의 `-wal` 없음 24~38회). 이때 **헬스는 `backlog` 가 아니라 `unknown`(`no_wal_evidence`)과 `caught_up`(`db_changed_within_grace`)을 오갔다**.
   - Litestream 이 **없고** 앱이 연결을 쥘 때(`CONN_MAX_AGE=None`): 1000 은 자라지 않고(6~7MB), 0 은 약 178 MB/분. 헬스는 `backlog`.
   - 프로필(`replicate -exec`)에서는 Litestream 이 PID 1 이라 '앱은 살고 Litestream 만 없는' 상태가 생기지 않는다. 그 두 경우는 감독 변형(`labapp-sup`)으로만 만들었다.
4. **Q4 — 로컬 L0 하나가 깨지면 컴팩션이 멈추고 원격 L0 가 선형으로 쌓이는데, 헬스는 내내 `caught_up` 이다.** 로컬 메타의 L0 파일 하나를 손상시키자 L1 컴팩션이 30초마다 `compaction failed`(0.5.18 `lz4: invalid source`, 0.5.17 `file checksum mismatch`)로 실패했고, 원격 L0 가 8분에 21 → 449개(0.5.18)·440개(0.5.17)로 늘었다. 헬스(42회 표본)는 모두 `caught_up`, boot 와 같은 원격 조회는 정상 TXID 를 냈다. 원격 L0 객체를 손상시킨 경우는 컴팩션이 멈추지 않았다(L0 는 보존 5분으로 약 270개에서 멈춤).
5. **곁가지 관측 — 감독 변형에서 Litestream 을 멈췄다 다시 띄우면 연결을 쥔 앱이 SIGBUS 로 죽을 때가 있었다.** `CONN_MAX_AGE=None` + Litestream 정지 + 재기동에서 재기동 약 1초 안에 uvicorn 이 SIGBUS 로 끝났다(180초 정지 7회 중 3회, 20초 정지 12회 중 1회. WAL 크기와 무관, OOM 아님). 연결을 닫는 앱에서는 16회 중 0회였다. 프로필에서는 생기지 않는 구성이고 원인은 미검증이다(§Q3 '앱 종료').

## 환경

- 호스트 macOS(arm64), colima 0.10.3(linux/arm64), Docker 29.5.2, Compose 5.5.1.
- 앱 이미지: 프로필 문서 Dockerfile 그대로(`python:3.13-slim` digest `sha256:e48f59cd…`), Python 3.13.18, SQLite 3.46.1, Django 6.1.3. Litestream 은 `LAB_LITESTREAM_VERSION` 으로 고른 릴리스 .deb, 결과에는 이미지 안의 `litestream version`(0.5.17·0.5.18)을 적었다.
- S3 는 SeaweedFS 4.48(toxiproxy 경유, 끊지 않음). 헬스 `REFRESH` 2초·`BACKLOG_GRACE` 10초.
- 프로필 문서의 compose·Dockerfile·`litestream.yml` 은 바꾸지 않았다. `wal_autocheckpoint` 0 은 `LAB_PRAGMAS` → `sqlite_database(pragmas=...)` → `init_command` 이고, 1000 은 설정을 더하지 않은 프로필 그대로다. 매 실행 `/lab/stats` 로 실제 연결의 값을 읽어 확인했다(모두 일치).
- 원자료: `lab/.out/`(git 무시) — 아래 표마다 파일 이름을 적었다. 측정 코드: `lab/test_walcp.py`, `lab/_walcpstat.py`, `lab/app/lab_tools/walcp_load.py`·`walcp_sup.py`·`lsctl.py`, 표는 `lab/walcp_report.py`. 실행은 `scripts/lab.sh walcp`([`lab/README.md`](../../lab/README.md#앱-wal_autocheckpoint-와-litestream-walcp-106)).

## 부하와 계측

- 부하(`walcp_load.py`, 같은 compose 네트워크의 별도 컨테이너): 쓰기 스레드 2개가 `POST /lab/write?n=10&size=3500`(행 10개 × 3,500바이트, 한 트랜잭션)을 합계 30 req/s 로, 읽기 스레드 2개가 커밋된 id 를 무작위로 읽는다. 행 데이터 약 1 MB/s. 90초에 27,020행(시드 포함 20행 차이 없음).
- 커밋된 행: 200 응답의 `first_id`·`last_id` 범위. 응답을 못 받은 쓰기는 `unknown` 으로 따로 센다(Q1 은 모두 0).
- 복원본: 볼륨 없는 `tool` 컨테이너가 `litestream restore` 로 `/tmp` 에 복원해 id 구간·`PRAGMA integrity_check` 를 낸다. 빠진 행은 '구멍'(복원본 최대 id 아래 — #1540 의 꼴)과 '꼬리'(최대 id 뒤 — 아직 올라가지 않은 지연)로 나눈다.
- WAL 표본(0.1초): `-wal` 크기·헤더 salt, `-shm` 의 `mxFrame`·`nBackfill`·`nBackfillAttempted`(잠금 없이 읽음). 'salt 변화'는 표본 간격 사이에 WAL 헤더가 다시 쓰였다는 것(되감김)의 **하한**이다. `nBackfill` 은 되감기면 0 으로 돌아가므로 '증가' 수는 작게 잡힌다.
- 시험 실행(60초, 각 1회)에서 자동 체크포인트가 실제로 걸리는 것(salt 변화 23~52회)을 먼저 확인했다. 그 실행에서 헬스가 부하 종료 1초 뒤 부하 중에 갱신한 `caught_up` 을 내어 꼬리 400행이 '빠짐'으로 보였다(복제 지연, `docker stop` 뒤 0). 본 측정은 부하가 끝난 뒤 갱신 2주기 + 1초를 쉬고 헬스를 본다.

## Q1. 프로필 구성에서 앱의 자동 체크포인트가 복제본의 커밋을 빠뜨리는가

방식:

- `steady`: 프로필 그대로의 `labapp`(`boot` → `litestream replicate -exec`). 90초 부하 → 헬스 `caught_up` → 복원본 대조 → `docker stop` → 다시 대조.
- `restart`: 감독 변형 `labapp-sup`(boot 는 같고, 그 뒤를 `walcp_sup.py` 가 맡아 uvicorn 과 `litestream replicate -config /etc/litestream.yml` 을 따로 띄움). 부하 30초에 Litestream 만 SIGTERM(정상 종료, 0.05초), 20초 뒤 재기동. 앱은 그동안 쓴다. `CONN_MAX_AGE=0`(프로필 기본).
- `restart-persist`: `restart` + `CONN_MAX_AGE=None`. 0 이면 Litestream 이 없는 동안 마지막 연결이 닫힐 때 `-wal` 이 지워진다(정지 중 '`-wal` 없는 표본' 11~25회). 연결을 쥐면 WAL 파일이 남고 재시작만 일어난다(0회) — 상류 #1540 코멘트(better-sqlite3, 기본 자동 체크포인트)의 꼴에 가깝다.

결과(빠진 행은 구멍/꼬리, 3회 각각):

| 판 | ckpt | 방식 | 커밋 행 | 빠진 행(caught_up 뒤) | 빠진 행(stop 뒤) | integrity | 정지 중 salt 변화 | 정지 중 `-wal` 없는 표본 |
|---|---|---|---|---|---|---|---|---|
| 0.5.18 | 1000 | steady | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | — | — |
| 0.5.18 | 0 | steady | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | — | — |
| 0.5.18 | 1000 | restart | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 37, 31, 33 | 25, 16, 17 |
| 0.5.18 | 0 | restart | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 27, 33, 31 | 19, 11, 16 |
| 0.5.18 | 1000 | restart-persist | 27,020, **15,330**(앱 SIGBUS), 27,020 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 13, 13, 14 | 0, 0, 0 |
| 0.5.18 | 0 | restart-persist | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 0, 0, 0 | 0, 0, 0 |
| 0.5.17 | 1000 | steady | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | — | — |
| 0.5.17 | 0 | steady | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | — | — |
| 0.5.17 | 1000 | restart | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 31, 31, 28 | 17, 17, 21 |
| 0.5.17 | 0 | restart | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 30, 30, 31 | 22, 23, 14 |
| 0.5.17 | 1000 | restart-persist | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 13, 14, 14 | 0, 0, 0 |
| 0.5.17 | 0 | restart-persist | 27,020 ×3 | 0/0 ×3 | 0/0 ×3 | ok ×3 | 0, 0, 0 | 0, 0, 0 |

원자료: `walcp-20261112-220234-replica.json`(0.5.18 steady·restart), `walcp-20261113-003152-replica.json`(0.5.18 restart-persist), `walcp-20261112-230450-replica.json`(0.5.17). 부하 오류는 모든 실행에서 0. 재기동 때 INFO 로그에 스냅샷 줄은 없었다(Litestream 이 재기동을 어떻게 이어 갔는지는 이 로그로 알 수 없다).

근거 등급: 재현함(빠지지 않음, 판·조건마다 3회).

한계: 정지는 20초·한 번, 부하는 약 1 MB/s 였다. 상류 코멘트의 꼴(정지 시점의 WAL 위치가 큼, 그 뒤 새 WAL 세대들이 그 위치까지 자라지 않음)을 의도해 맞추지 않았다 — 이 부하에서 정지 시점 WAL 은 6~9MB 였다. SIGKILL 정지, 디스크가 느린 경우, 수십 분 단위 운용은 재지 않았다.

### 상류 #1540 과 무엇이 다른가

상류 이슈 원문(2026-11-12 읽음)의 재현은 테스트 하네스가 Litestream 의 읽기 트랜잭션을 **직접 놓은** 뒤 121행을 쓰고 외부 `wal_checkpoint(TRUNCATE|RESTART)` 를 하고, 데이터 테이블 페이지를 다시 쓰지 않도록 heartbeat 테이블만 쓴 뒤 sync 한다. 본문은 그것이 '모든 외부 체크포인트가 Litestream 의 읽기를 우회한다는 주장이 아니다'라고 적는다. 이 패키지 구성과 다른 점:

| | 상류 재현 | 이 측정 |
|---|---|---|
| 읽기 잠금 | 하네스가 놓음 | Litestream 이 살아 있으면 쥠(얼린 동안 앱 1000 도 되감지 못함 — Q2). 놓이는 것은 Litestream 이 없을 때뿐 |
| 체크포인트 | 외부 `TRUNCATE`/`RESTART` | 앱의 자동(PASSIVE) 체크포인트와 마지막 연결 닫기 체크포인트 |
| 그 뒤 쓰기 | 다른 테이블만(빠진 페이지를 다시 쓰지 않게) | 같은 테이블에 계속 |
| 프로필에서 가능한가 | — | `replicate -exec` 라 Litestream 만 없는 상태는 프로필에서 생기지 않는다. 감독 변형으로만 만들었다 |

코멘트(donald-w, 0.5.17, 파일 복제본, better-sqlite3)는 하네스 없이 'Litestream SIGTERM → 앱이 계속 쓰며 자동 체크포인트로 WAL 을 여러 번 재시작 → 재기동 뒤 스냅샷 없이 이어 감 → 최신 시점 복원이 `integrity_check full` 실패'를 3/3 으로 보고했다. `restart-persist` 가 그 꼴에 가장 가깝지만(정지 중 WAL 파일이 남고 13~14번 재시작), 이 측정에서는 12회(두 판 각 6회) 모두 행이 빠지지 않았고 `integrity_check` 도 `ok` 였다. 차이로 보이는 것: 그 보고의 정지 시점 WAL 위치(약 16MB, 15–20k 페이지의 쓰기 폭주 중) 대 이 측정의 6~9MB, S3 대 파일 복제본, `integrity_check full` 대 `PRAGMA integrity_check`(같은 검사다). 어느 차이가 결정적인지는 재지 않았다(미검증).

## Q2. Litestream 이 살아 있을 때 앱의 자동 체크포인트는 일어나는가, 누가 WAL 을 되감는가

90초 부하, 3회 각각. 'nBackfill 증가'는 체크포인트가 프레임을 DB 로 옮기는 것을 본 표본 간격 수, '막힌 표본'은 `nBackfillAttempted > nBackfill`(읽는 쪽 때문에 끝까지 못 옮김)인 표본 수(전체 약 850).

| 판 | 구성 | ckpt | salt 변화 | nBackfill 증가 | 막힌 표본 | WAL 최대 MB |
|---|---|---|---|---|---|---|
| 0.5.18 | 프로필(steady) | 1000 | 43, 44, 43 | 3, 1, 3 | 4, 1, 2 | 8.9, 6.3, 8.6 |
| 0.5.18 | 프로필(steady) | 0 | 44, 42, 43 | 0, 6, 2 | 7, 7, 4 | 6.0, 11.9, 8.8 |
| 0.5.17 | 프로필(steady) | 1000 | 43, 44, 43 | 4, 2, 3 | 4, 8, 3 | 8.6, 8.8, 8.3 |
| 0.5.17 | 프로필(steady) | 0 | 44, 43, 42 | 1, 2, 4 | 2, 6, 4 | 8.8, 9.0, 11.3 |
| 0.5.18 | Litestream 없음(`labapp-plain`) | 1000 | 129 | 0 | 153 | 0.1 (`-wal` 없는 표본 88) |
| 0.5.18 | Litestream 없음 | 0 | 113 | 1 | 122 | 0.2 (69) |
| 0.5.17 | Litestream 없음 | 1000 | 142 | 1 | 144 | 0.2 (85) |
| 0.5.17 | Litestream 없음 | 0 | 142 | 1 | 144 | 0.1 (86) |

원자료: `walcp-20261112-220234-{replica,plain}.json`, `walcp-20261112-230450-{replica,plain}.json`(plain 은 판과 무관한 구성이지만 실행마다 한 번 쟀다).

답:

- **누가 되감는가**: Litestream 이 살아 있을 때 되감김 수가 ckpt 1000·0 에서 같다(42~44). 앱이 체크포인트를 하지 않아도(0) 같은 만큼 되감기므로 되감김은 Litestream 의 체크포인트가 만든다(재현함). 앱 1000 이 따로 되감김을 더하는 것은 보이지 않았다.
- **Litestream 의 읽기 잠금이 앱의 체크포인트를 막는가**: 막는다. Q3 `hang`(Litestream 을 SIGSTOP, 읽기 잠금을 쥔 채)에서 앱 1000 이어도 180초 동안 salt 변화 0·WAL 535MB 였고, 같은 1000 에서 Litestream 프로세스가 없고 연결을 쥔 `dead-persist` 는 WAL 6~7MB·salt 변화 127 이었다(재현함).
- Litestream 없음(`labapp-plain`)은 요청마다 연결을 닫으므로(`CONN_MAX_AGE=0`) 마지막 연결 닫기 체크포인트가 `-wal` 을 지운다. ckpt 0 에서도 같다 — `wal_autocheckpoint=0` 은 이 체크포인트를 막지 않는다(재현함).
- Litestream 로그(INFO)에는 체크포인트 줄이 없다. 0.5.18 은 WAL 이 약 500MB(`threshold=499999112`)를 넘으면 `forcing truncate checkpoint` 또는 `wal restarted by passive checkpoint, skipping truncate checkpoint` 를 한 줄 남겼다(Q3).

한계: 'salt 변화'는 0.1초 표본의 하한이다. 앱 1000 의 PASSIVE 체크포인트가 Litestream 의 것과 같은 간격에 겹쳤다면 구분되지 않는다 — '앱이 되감김을 더하지 않았다'는 횟수가 같다는 관측이지 체크포인트 호출을 센 것이 아니다.

## Q3. Litestream 이 멈추면 WAL 은 얼마나 자라고 지연은 어떤가

부하 20초 뒤 Litestream 을 180초 멈추고(`hang`: 프로필 그대로에서 `docker compose kill -s SIGSTOP` 으로 PID 1 인 Litestream 을 얼림, 재개는 SIGCONT / `dead`·`dead-persist`: 감독 변형에서 SIGTERM, 재개는 재기동) 10초 더 쓴다. 증가율은 정지 구간 `-wal` 크기의 최소제곱 기울기, 지연은 정지 직전과 정지 끝의 5초 구간 p50/p95(ms).

| 판 | 정지 | ckpt | `CONN_MAX_AGE` | WAL MB/분 | 정지 중 WAL 최대 MB | 정지 중 salt 변화 | 쓰기 p50/p95 전 → 끝 | 읽기 p50/p95 전 → 끝 | 정지 중 헬스(10초 표본 18개) | 재개 뒤 |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.5.18 | hang | 1000 | 0 | 178.2 | 533.9 | 0 | 4/8 → 8/11 | 3/5 → 3/5 | backlog 16 | caught_up, 빠짐 0 |
| 0.5.18 | hang | 0 | 0 | 178.0 | 533.4 | 0 | 5/8 → 5/7 | 3/5 → 3/5 | backlog 16 | caught_up, 빠짐 0 |
| 0.5.18 | dead | 1000 | 0 | −0.0 | 5.3 | 312 | 5/7 → 6/10 | 3/5 → 3/7 | unknown 11, caught_up 7 | caught_up, 빠짐 0 |
| 0.5.18 | dead | 0 | 0 | 0.0 | 0.7 | 320 | 6/13 → 23/78 | 4/7 → 9/40 | unknown 11, caught_up 7 | caught_up, 빠짐 0 |
| 0.5.18 | dead-persist | 1000 | None | 0.0 | 6.3 | 127 | 6/14 → 5/17 | 4/8 → 3/6 | backlog 16 | caught_up, 빠짐 0 |
| 0.5.18 | dead-persist | 0 | None | 177.9 | 532.4 | 0 | 5/9 → 5/8 | 3/6 → 3/5 | backlog 16 | **앱 SIGBUS** |
| 0.5.17 | hang | 1000 | 0 | 177.9 | 535.1 | 0 | 4/7 → 9/12 | 3/5 → 3/5 | backlog 16 | caught_up, 빠짐 0 |
| 0.5.17 | hang | 0 | 0 | 177.9 | 535.2 | 0 | 5/8 → 5/10 | 3/5 → 3/6 | backlog 16 | caught_up, 빠짐 0 |
| 0.5.17 | dead | 1000 | 0 | −0.0 | 0.9 | 312 | 5/9 → 6/9 | 3/6 → 3/6 | unknown 10, caught_up 8 | caught_up, 빠짐 0 |
| 0.5.17 | dead | 0 | 0 | 0.0 | 0.4 | 300 | 4/8 → 7/11 | 3/5 → 3/7 | unknown 10, caught_up 7, backlog 1 | caught_up, 빠짐 0 |
| 0.5.17 | dead-persist | 1000 | None | 0.0 | 7.1 | 127 | 5/33 → 5/12 | 3/5 → 3/6 | backlog 16 | **앱 SIGBUS** |
| 0.5.17 | dead-persist | 0 | None | 178.7 | 538.2 | 0 | 5/8 → 5/10 | 3/5 → 3/5 | backlog 16 | **앱 SIGBUS** |
| 0.5.18 | dead-persist | 1000 | None | 0.0 | 6.4 | 127 | 5/8 → 5/31 | 3/5 → 3/5 | backlog 16 | caught_up, 빠짐 0 (재측정) |
| 0.5.18 | dead-persist | 0 | None | 178.0 | 536.2 | 0 | 4/7 → 5/22 | 3/5 → 3/5 | backlog 16 | caught_up, 빠짐 0 (재측정 2회 — `230029`·`003152`) |

원자료: `walcp-20261112-220234-stall.json`(0.5.18), `walcp-20261112-230029-stall.json`(0.5.18 `dead-persist`·0 재실행), `walcp-20261113-003152-stall.json`(0.5.18 `dead-persist` 재측정), `walcp-20261112-230450-stall.json`(0.5.17). 정지 중 헬스의 나머지 표본 2개는 정지 직후의 `unknown`(`not_checked`)·`caught_up`(grace 안)이다.

답:

- **얼마나 자라나**: 읽기 잠금이 쥐어진 채 멈추면(얼음) `wal_autocheckpoint` 와 무관하게 쓰기 양만큼 자란다 — 이 부하(행 데이터 약 1 MB/s)에서 약 178 MB/분(3분에 535MB, 프레임 헤더·인덱스 페이지 포함). 앱의 체크포인트가 할 수 있는 일이 없다(재현함).
- Litestream 프로세스가 없으면 앱이 되감는다: 연결을 닫는 앱(프로필 기본 `CONN_MAX_AGE=0`)은 ckpt 와 무관하게, 연결을 쥐는 앱은 ckpt 1000 일 때. **`wal_autocheckpoint=0` 이 WAL 을 무한히 자라게 하는 것은 '연결을 쥔 앱 + Litestream 프로세스 없음' 조합에서만 관측됐다**(재현함).
- **지연**: 535MB WAL 에서도 쓰기·읽기 p95 는 수 ms 변화였다(이 부하·이 디스크). `dead`·0(0.5.18)의 쓰기 p95 13 → 78ms 는 0.5.17 의 같은 조건(8 → 11ms)에서 재현되지 않았다(1회씩).
- **헬스**: 얼음·연결 유지에서는 `backlog` 로 알렸다. 연결을 닫는 앱에서 Litestream 이 없을 때는 `backlog` 가 아니라 `unknown`(`no_wal_evidence`)과 `caught_up`(`db_changed_within_grace`)을 오갔다(두 판, 재현함). `-wal` 이 수시로 지워져 WAL 근거가 없어지거나 grace 안의 새 변경으로 다시 시작되기 때문으로 본다(헬스 판정 코드와 맞는 설명, 이 경로를 따로 단위 시험하지는 않았다).

### 앱 종료(SIGBUS, 감독 변형에서만)

연결을 쥔 앱(`CONN_MAX_AGE=None`)에서 Litestream 을 재기동하면 약 1초 안에 감독 프로세스가 `app exited rc=-7`(uvicorn 이 SIGBUS 로 끝남, 컨테이너 rc 249)을 남겼다. `docker inspect` 의 `OOMKilled` 는 false, 재개 직전 `/data` 디스크 사용 40%. 횟수: 0.5.18 ckpt 0 에서 3회 중 1회, ckpt 1000 에서 2회 중 0회, 0.5.17 ckpt 1000·0 각 1회 중 1회 — `dead-persist` 7회 중 3회. 20초 정지(`restart-persist`)에서도 0.5.18 ckpt 1000 에서 6회 중 1회 났다(재기동 t=50.1초 직후, 0.5.17 6회는 없음) — 12회 중 1회. 연결을 닫는 앱(`restart`·`dead` 16회)과 얼음(`hang` 4회)에서는 없었다. 앱과 Litestream 이 함께 끝나므로 정지 중 쓴 행이 복제되지 않았다(0.5.17 ckpt 0 에서 꼬리 54,510행 — 로컬 볼륨에는 남음).

프로필에서는 Litestream 이 PID 1 이라 '앱은 사는데 Litestream 만 재기동' 이 생기지 않는다. 원인(무엇이 매핑된 파일을 줄였는지)은 재지 않았다(미검증). 원자료의 `app_log_tail` 에 남는다. 0.5.17 ckpt 0 의 끝(접근 로그 제외):

```text
[sup] 15:08:37 litestream exited rc=0
[sup] 15:11:37 litestream started pid=252463
time=2026-11-12T15:11:38.011Z level=INFO msg=litestream version=0.5.17 level=""
time=2026-11-12T15:11:38.012Z level=INFO msg="initialized db" path=/data/app.sqlite3
time=2026-11-12T15:11:38.015Z level=INFO msg="starting L0 retention monitor" system=store interval=15s retention=5m0s
[sup] 15:11:39 app exited rc=-7
time=2026-11-12T15:11:39.091Z level=INFO msg="signal received, litestream shutting down" signal=terminated
```

## Q4. 컴팩션이 멈추면 원격 L0 가 쌓이고, 그때 헬스는 무엇을 말하는가

프로필 그대로, 쓰기 5행씩 약 1초 간격(L0 가 쓰기마다 생기게). 12초쯤 손상(64바이트 뒤집기)을 넣고 약 11초마다 레벨별 원격 객체 수(`s3_objects.py levels`)·헬스·boot 와 같은 원격 조회(`replica.py --no-restore`)를 표본했다. 객체 경로는 `<prefix>/<레벨 4자리>/<min>-<max>.ltx`(0000 = L0, 0009 = 스냅샷).

| 판 | 손상 대상 | 시간 | L0 수 (첫 → 끝) | L1 | Litestream 로그 | 헬스 | 원격 조회 | 끝의 복원 |
|---|---|---|---|---|---|---|---|---|
| 0.5.18 | 원격 L0 객체(txid 0xb, L1 로 합쳐지기 전) | 10분 | 21 → 269(보존 5분으로 약 270 에서 멈춤) | 2 → 22 | 경고·오류 0 | caught_up 52/53 | 정상 | ok, 2,700행 |
| 0.5.18 | 로컬 메타의 L0 파일(txid 0xb) | 8분 | 21 → 449 | 0 그대로 | 30초마다 `ERROR compaction failed … lz4: invalid source or destination buffer too short`, 19줄 | caught_up 42/43 | 정상(TXID 449) | ok, 2,200행 |
| 0.5.17 | 로컬 메타의 L0 파일(txid 0xb) | 8분 | 21 → 440 | 2 그대로 | 30초마다 `ERROR compaction failed … file checksum mismatch`, 16줄 | caught_up 42/43 | 정상(TXID 449) | ok, 2,200행 |

원자료: `walcp-20261112-214202-l0.json`, `walcp-20261112-215304-l0.json`, `walcp-20261112-230450-l0.json`. 헬스의 나머지 1개는 첫 표본의 `unknown`(`not_checked`).

답:

- 로컬 L0 하나가 깨지면 L1 컴팩션이 계속 실패하고 L0 가 보존 기간과 관계없이 쌓인다(쓰기 1회/초에서 분당 약 55개). 원격 L0 를 깨뜨렸을 때는 컴팩션이 계속됐다 — 컴팩션은 로컬 L0 를 읽는 것으로 보인다(관측, 코드는 보지 않음).
- 그동안 헬스는 `caught_up` 이었다. 헬스는 로컬·원격 TXID 를 비교하고, L0 업로드는 계속되므로 둘이 같다(재현함). boot 의 원격 조회도 최대 TXID 를 정상으로 낸다.
- 8분 뒤 복원은 성공했다(L0 사슬로 복원 — 원격의 그 L0 는 손상 전에 올라간 정상 사본이다).

시간 상자(60분) 안에서 끝났다(약 30분 사용: 원격 손상 10분, 로컬 손상 0.5.18 8분·0.5.17 8분). 한계: 수 시간 단위의 L0 누적(상류 보고의 63,000개), 그때의 복원 시간·비용, 손상이 업로드 전에 생기는 경우는 재지 않았다.

## 운영자에게

- 프로필 그대로(`replicate -exec`)라면 앱의 `wal_autocheckpoint` 를 바꿀 근거는 이 측정에서 나오지 않았다: 1000·0 모두 빠진 커밋 0, Litestream 이 살아 있으면 되감김도 같다.
- WAL 이 자라는 경우는 'Litestream 이 읽기 잠금을 쥔 채 멈춘 것'이고, 그때는 앱 설정과 무관하게 쓰기 양만큼 자란다(이 부하에서 약 178 MB/분). 헬스는 이것을 `backlog` 로 알렸다.
- 컴팩션이 멈춰도 헬스는 `caught_up` 이다. 원격 L0 개수나 Litestream 의 `compaction failed` 오류 줄을 따로 봐야 알 수 있다.

## 후속 제안 (이슈는 오케스트레이터가 정한다)

1. **권장 설정에 `wal_autocheckpoint` 를 넣을지**: 넣지 않는 쪽의 근거만 나왔다. 프로필에서 1000·0 의 차이(빠진 커밋·되감김·WAL 크기)가 없었고, 0 은 '연결을 쥔 앱 + Litestream 없음'에서 WAL 을 무한히 키웠다(178 MB/분). 표의 근거 칸을 이 기록으로 바꾸고 값은 그대로 둔다.
2. **헬스·doctor 에 WAL 크기를 넣을지**: 헬스가 `backlog` 를 이미 내는 경우(얼음)에 WAL 크기는 같은 사실을 다른 모양으로 말한다. 다만 `CONN_MAX_AGE=0` 앱에서 Litestream 이 없을 때 헬스가 `backlog` 를 내지 않은 것(위 Q3)은 따로 볼 만하다 — 이 경우 WAL 크기도 작아 WAL 크기 임계값으로는 잡히지 않는다. 임계값 후보를 낼 근거: 정상 운용 WAL 최대 6~12MB(이 부하), 얼음 3분 535MB, 0.5.18 의 강제 TRUNCATE 문턱 약 500MB.
3. **헬스·doctor 에 원격 L0 개수를 넣을지**: 넣을 근거가 있다. 컴팩션이 멈춘 8분 동안 헬스는 `caught_up` 이었고 L0 는 분당 약 55개 늘었다. 정상 운용에서 L0 는 보존 5분 동안 쌓인 수(이 쓰기 패턴에서 약 270)에서 멈췄다. 임계값 후보는 '보존 시간 × 업로드 빈도'의 몇 배 또는 '가장 오래된 L0 의 나이 > 보존 + L1 주기의 몇 배'다(후자는 쓰기 빈도와 무관하다 — 재지 않음).

## 미검증

- 정지 20초보다 긴 Litestream 재시작, SIGKILL 재시작, 정지 시점 WAL 이 큰 경우의 Q1.
- 0.1초보다 짧은 되감김의 정확한 횟수, 앱 1000 의 체크포인트 호출 수.
- 감독 변형의 SIGBUS 원인.
- amd64, 다른 디스크·S3 구현, 다른 SQLite 판.
