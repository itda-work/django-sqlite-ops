# 헬스가 멈춘 복제 파이프라인을 `caught_up` 으로 감추지 않게 (#115, 2026-11-13)

#106 에서 헬스가 놓친 두 경우를 고친 근거다. 하나는 로컬 L0 하나가 깨져 L1 컴팩션만 멈출 때 `caught_up` 이 나온 것이고, 다른 하나는 `CONN_MAX_AGE=0` 앱에서 Litestream 프로세스가 없을 때 `backlog` 대신 `unknown` 과 `caught_up` 이 번갈아 나온 것이다([#106 기록](lab-2026-11-12-wal-autocheckpoint.md)). 설계는 [DESIGN §7](../DESIGN.md#7-헬스)의 '컴팩션 진행'·'pending 키 통일' 절이다.

환경: macOS(arm64) 로컬 Litestream 0.5.17(Homebrew)·0.5.18(릴리스 바이너리), 파일 복제본. 랩은 colima linux/arm64, SeaweedFS 4.48. Python 3.14.7, SQLite 3.53.1(uv).

## 소스 확인

출처는 `github.com/benbjohnson/litestream` 태그 `v0.5.17`·`v0.5.18` 이다(2026-11-13 에 읽음).

- **기본값은 두 판이 같다.** `compaction_level.go` 의 L1 은 30초, L2 는 5분, L3 은 1시간이다. `PrevCompactionAt` 은 `now.Truncate(Interval)` 이라 벽시계 30초 경계마다 돈다. `store.go` 의 L0 보존은 5분이고, 점검은 15초마다, 스냅샷은 24시간마다다.
- **L0 보존은 L1 끝에서 멈춘다.** `db.go` 의 `EnforceL0RetentionByTime` 은 L1 파일의 최대 `MaxTXID` 를 컴팩션된 끝으로 본다. 그보다 큰 L0 는 지우지 않고 `not_compacted` 로 센다. 그래서 컴팩션이 멈추면 L0 가 쌓인다.
- **L1 끝은 줄지 않는다.** `compactor.go` 의 `Compact()` 는 파일을 지우지 않는다. 보존 정리인 `EnforceRetentionByTXID`(293–316)와 다른 삭제 경로는 모두 레벨의 마지막 파일을 남긴다. L2~L8 은 L(n−1) 에서만 만들어지므로 L1 끝을 넘을 수 없다.
- **스냅샷(L9)은 컴팩션된 끝이 아니다.** `db.go` 2940 의 `WriteLTXFile(ctx, SnapshotLevel, 1, pos.TXID, r)` 은 DB 의 현재 위치에서 새로 쓴다. 그래서 L9 를 빼고 레벨 1~8 의 최대를 기준으로 삼는다. L9 를 넣으면 하루 한 번 스냅샷이 정지를 가린다.
- **`ltx -json` 의 `timestamp` 는 원격의 시계다.** 값은 `CreatedAt` 이다. S3 에서는 `LastModified` 이고, 메타데이터 시각이 있으면 그것을 쓴다. 파일 복제본에서는 파일 mtime 이다. 컴팩션 지연은 한 목록 안의 두 시각끼리 빼므로 로컬 시계가 끼지 않는다.
- **최신 L0 가 깨질 때 판이 갈리는 이유 (0.5.18, 코드상 확인).** 0.5.18 의 `db.go` 는 `DB` 에 `lastLTXPage` 필드를 둔다(82). sync 가 L0 를 쓸 때 마지막 페이지를 메모리에 남기고(1946), 다음 동기화의 '마지막 페이지 일치' 검사는 그 메모리 사본과 비교한다(2369–2371). 그래서 디스크의 L0 가 깨져도 sync 가 이어진다. 0.5.17 은 같은 검사에서 직전 L0 를 디스크에서 다시 읽다가 실패한다. 무엇으로 그 동작을 바꿨는지(커밋·PR)는 찾지 않았다.

## 확인 실험 (기획 단계, 로컬 0.5.17, 기본 levels, 각 1회)

임시 디렉터리에서 `litestream replicate` 를 띄웠다. 연결을 쥔 sqlite3 로 1초마다 2,000바이트 행 하나를 쓰고, 10초마다 `ltx -level all -json` 으로 컴팩션 지연을 계산했다. 컴팩션 지연은 가장 새 객체 시각에서 가장 오래된 미컴팩션 L0 시각을 뺀 값이다.

| 실험 | 손상한 로컬 L0 | 정상 지연(표본) | 손상 뒤 | Litestream ERROR |
|---|---|---|---|---|
| 1 | **최신** L0 (t=200초) | 3~25초 톱니(19) | L0 기록이 멈췄다. 원격·로컬 최대 TXID 가 0xc8, L1 끝이 0xb9 에서 고정됐다. 새 객체가 없어 지연은 230초 동안 14초 그대로였다. L0 수는 보존 정리로 200 → 81 | `sync error … ltx file corrupted`, `cannot verify wal state`(백오프 1초 → 1분 4초), `compaction failed` 5줄 |
| 2 | 최신이 **아닌** 미컴팩션 L0 (t=93초) | 7~27초 톱니(9) | 컴팩션만 멈췄다. L0 업로드는 계속됐고(0x63 → 0x1cf) L1 끝은 0x49 에서 고정됐다. 지연은 25초 → 387초로 초당 약 1초씩 늘었고, 미컴팩션 L0 는 26 → 390개. 손상 288초 뒤 300초를 넘었다 | `compaction failed … lz4: invalid source or destination buffer too short` 13줄(30초마다) |

결론과 기본값의 근거는 다음과 같다.

- 정상 지연은 L1 주기에 묶인다. 28표본에서 최대 27초였다.
- 컴팩션이 멈추면 지연은 벽시계와 거의 같은 속도로 는다.
- `COMPACTION_GRACE` 기본값 300초는 L1 기본 주기의 10배이고, L0 보존 5분과 같다.
- L0 기록까지 멈추는 경우(실험 1)는 지연으로 잡지 못한다. 이 경우는 WAL 근거가 `db_not_replicated` 로 잡는다(아래 실제 테스트에서 재현함).
- 다른 쓰기 빈도에서는 재지 않았다.

## 수정 전에 실패한 테스트

배선(파서·설정·`Sample.ltx`)만 넣고 판정 변경과 키 통일을 빼고 돌렸다.

- 단위 테스트 11건이 실패했다.
  - 지연 412초에도 `('caught_up', 'in_sync')` 가 나왔다.
  - 근거가 번갈아 나오면 `(0, caught_up)`, `(10, unknown no_wal_evidence)`, `(20, caught_up)` … 순서로 다시 셌다.
- 실제 Litestream 0.5.17 에서는 두 경우가 `timed out` 으로 실패했다.
  - 미컴팩션 L0 를 손상한 뒤 60초 내내 `('caught_up', 'db_changed_within_grace')` 였다.
  - Litestream 이 없을 때(요청마다 연결을 닫고, 겹치는 읽기 연결이 있음) `unknown/no_wal_evidence` 와 `caught_up/db_changed_within_grace` 가 번갈아 나왔다.
- 최신 L0 손상(0.5.17)은 수정 전에도 통과했다. 기존 WAL 근거가 잡는 경우다.
- 겹치는 연결이 없는 '연결 닫기' 테스트는 수정 전에도 통과했다. uv Python 의 SQLite 는 닫을 때마다 `-wal` 을 지우므로 표본이 늘 파일 시각 근거로만 나온다. 그래서 1.5초 동안 연결을 쥐는 읽기 스레드를 더해 두 근거가 실제로 번갈아 나오게 했다. 테스트는 번갈아 나왔다는 사실도 단언한다.

## 실제 Litestream 테스트 (`tests/test_health_pipeline.py`, L1 4초, 파일 복제본)

| 테스트 | 0.5.17 | 0.5.18 |
|---|---|---|
| 미컴팩션 L0 손상 → `backlog/compaction_stalled` | 3/3 통과 | 3/3 통과 |
| 최신 L0 손상 → 판별 표 `LATEST_L0_CORRUPTION` | `backlog/db_not_replicated`, 3/3 | `backlog/compaction_stalled`, 3/3 |
| Litestream 없음 + 연결 닫기 + 겹치는 읽기 → `backlog/db_not_replicated` | 3/3 통과 | 3/3 통과 |

한 번 도는 데 약 28초 걸린다. 최신 L0 를 손상한 뒤의 Litestream ERROR 줄은 판마다 다르다(재현함).

- 0.5.17: `msg="sync error" … error="cannot verify wal state: last page match: decode ltx page: decompress block: lz4: invalid source or destination buffer too short"`. 이 뒤로 L0 기록이 멈춘다. `compaction failed` 줄도 나온다.
- 0.5.18: `sync error` 는 0줄이다. `msg="compaction failed" … level=1 error="write ltx file: read page header 0: decompress block: lz4: …"` 만 나오고, L0 기록은 이어진다(로컬 L0 0x7 → 0x9).

두 판 모두 `caught_up` 이 아니다. 판별 표에 없는 판이면 테스트는 실패한다.

0.5.18 의 최신 L0 테스트는 처음에 전체 실행 안에서 1회 실패했다(60초 내내 `caught_up`, `compaction failed` 0줄). 손상하기 전에 그 L0 가 이미 L1 로 컴팩션돼 있었기 때문이다. 0.5.18 은 디스크의 L0 를 다시 읽지 않으므로 컴팩션이 끝난 L0 의 손상은 드러나지 않는다. 그래서 다음 조건을 넣었다.

- 마지막 L1 경계 0.5초 뒤에 생긴 L0 만 깨뜨린다.
- 다음 경계까지 0.5초 넘게 남았을 때만 깨뜨린다.

이 조건을 넣은 뒤에 다시 1회 실패했다. 그 실행에서 Litestream 의 1초 tick 은 L1 경계 0.44초 뒤에 고정돼 있었다(L0 mtime 의 위상으로 확인). 경계 0.3초 뒤에 쓰기를 멈추면 마지막 L0 가 매번 그 tick 에 생겨 조건을 넘지 못했다. 쓰기를 경계 1초 뒤까지 이어 가게 고친 뒤 0.5.18 은 10/10 통과했다. 0.5.17 에서는 실제 테스트 3건이 5/5 통과했다.

전체 `pytest`(0.5.17, `REQUIRE_LITESTREAM=1`): 3868 passed, 3 skipped(대소문자 무시 볼륨 2, 판별로 대신하는 1).

## 랩 L9a·L9b (colima arm64, `scripts/lab.sh run all`)

두 판에서 각각 한 번 돌렸다. `run all` 의 15건(L1–L9, P1·P2)이 모두 통과했다. 0.5.17 은 18분 32초, 0.5.18 은 17분 36초 걸렸다. 끝난 뒤 `io.itda.dso-lab=1` 라벨의 컨테이너와 볼륨은 0개였다. 원자료는 `lab/.out/results-20261113-080310.jsonl`(0.5.17)과 `results-20261113-082252.jsonl`(0.5.18)이며 저장소에 넣지 않는다.

L9a 는 REFRESH 2초, `COMPACTION_GRACE` 60초로 돌렸다.

| 판 | 소요 | 정상 구간(40초) | 손상한 L0 | 손상 뒤 첫 `backlog/compaction_stalled` | 로그 |
|---|---|---|---|---|---|
| 0.5.17 | 106.8초 | 모두 `caught_up`(`db_changed_within_grace`·`in_sync`) | `…2e`, L1 경계 3.6초 뒤, 새 L0 2개 중 앞의 것 | 58.5초 | `compaction failed` 2줄, `sync error` 0줄, 쓰기 오류 0 |
| 0.5.18 | 94.2초 | 모두 `caught_up`(`db_changed_within_grace`) | `…2a`, L1 경계 13.9초 뒤, 새 L0 12개 중 끝에서 둘째 | 48.4초 | `compaction failed` 2줄, `sync error` 0줄, 쓰기 오류 0 |

첫 `backlog` 가 grace 60초보다 이르다. 지연은 손상 시각이 아니라 가장 오래된 미컴팩션 L0 의 시각부터 재기 때문이다. 손상한 L1 구간에서 그 L0 는 손상보다 먼저 생겨 있었다. 손상 뒤 `backlog` 전에는 `caught_up` 만 나왔다. 0.5.17 에서는 그 사이에 `local_ahead_within_grace` 가 한 번 섞였다. S3(SeaweedFS) 복제본에서도 시각은 원격 `LastModified` 로 계산됐다.

L9b 는 REFRESH 2초, `BACKLOG_GRACE` 10초, `CONN_MAX_AGE=0` 으로 돌렸다.

| 판 | 소요 | Litestream 정지 뒤 첫 `backlog/db_not_replicated` | 정지 동안 나온 코드 | 그 뒤 다른 상태 | 재기동 뒤 `caught_up` |
|---|---|---|---|---|---|
| 0.5.17 | 154.2초 | 12.1초 | `no_wal_evidence` ↔ `db_changed_within_grace` 가 번갈다가 `db_not_replicated` | 없음(60초 유지) | 2.0초 |
| 0.5.18 | 153.9초 | 12.1초 | 위와 같고 `local_ahead_within_grace` 가 더 나옴 | 없음(60초 유지) | 4.0초 |

#106 Q3 `dead` 에서는 같은 조건에서 `unknown` 과 `caught_up` 만 나왔다. 이번에는 근거가 번갈아 나와도 이어 세어 grace 뒤 `backlog` 가 되고, Litestream 이 없는 동안 그대로 유지된다. 부하 오류는 0건이다.

## 미검증

- L1 주기를 바꾼 배포에서 '`COMPACTION_GRACE` 는 L1 주기의 10배 이상' 규칙이 맞는지. 기본 30초만 쟀다.
- 실제 클라우드 S3·R2 에서의 정상 지연 폭. SeaweedFS 와 파일 복제본에서만 쟀다.
- amd64.
- 유휴 중 컴팩션 정지. 새 객체가 없으면 지연이 늘지 않아 다음 쓰기까지 보이지 않는다. 받아들인 한계다.
- 원격 L0 객체나 L1 이상의 손상.
- 0.5.18 이 최신 L0 손상을 넘긴 뒤 만든 복원본이 온전한지.
- 0.5.18 에서 `lastLTXPage` 가 들어온 커밋.
