# 회귀 랩 실측: 조회 타임아웃보다 긴 S3 단절 (2026-10-25, #65)

`lab/` 의 L8c·L8d 로 헬스 원격 조회 타임아웃(30초)보다 긴 S3 단절(90초)에서 헬스가 어떻게 바뀌는지, 매달린 `litestream ltx` 가 남는지, Litestream 이 무엇을 기록하는지 쟀다. 수치는 모두 아래 실행에서 나온 실제 값이다. 이전 L8a·L8b(20초 단절)는 [`lab-2026-10-08.md`](lab-2026-10-08.md) 에 있다.

## 환경

- 호스트: macOS(arm64) + colima(linux/arm64). 같은 VM 에서 다른 프로젝트 컨테이너가 돌고 있었다(부하는 통제하지 않았다).
- Docker 29.5.2, Docker Compose 5.5.1.
- 앱 이미지: `single-server` 문서 Dockerfile 그대로(`python:3.13-slim` = `sha256:bf44cdfc…`, Litestream 0.5.17). 패키지는 작업 트리 wheel(커밋 `793dbf1` 위 작업 트리, 제품 코드 변경 없음).
- S3: SeaweedFS 4.48, 장애 주입: toxiproxy 2.12.0(랩 README 의 digest 고정 그대로).
- 헬스: `LAB_HEALTH_REFRESH=2`, `LAB_HEALTH_GRACE=10`. 원격 조회 타임아웃은 `boot/litestream.py` 의 `DEFAULT_LTX_TIMEOUT` 30초(헬스 설정으로 바꿀 수 없음).
- 실행
  - **1회차**: `scripts/lab.sh run L8` — L8a·L8b·L8c·L8d 4 passed, 5분 21초. `lab/.out/run-20261025-211705.log`, `results-20261025-211706.jsonl`, 콘솔 `issue65-run1.console`.
  - **2회차**: 로그 요약에 모든 Litestream 줄 수를 더한 뒤 `scripts/lab.sh run "L8c or L8d"` — 2 passed, 3분 39초. `run-20261025-212318.log`, `results-20261025-212318.jsonl`, `issue65-run2.console`.
  - **1·2회차의 L8d 는 `timeout` toxic 을 `stream` 없이 하나만 넣었다.** toxiproxy 2.12.0 README 에서 `stream` 의 기본값은 `downstream`(server → client)이다. 그래서 요청(client → server)은 SeaweedFS 에 닿고 응답만 버려지는 '응답 유실'이었다(리뷰 1 P2-2). 헬스 쪽 결론(응답이 오지 않아 `ltx` 가 30초 매달림)은 같았다. 하지만 복구 시간·복제본·Litestream 로그는 완전한 무응답 단절의 근거가 아니다. 아래 결과 표의 L8d 는 3·4회차의 양방향 값만 쓴다.
  - **3회차(대표)**: 리뷰 1 반영(L8d 를 `downstream`·`upstream` 두 toxic 으로, `ltx` 표본 하한, 복구 전 로그 읽기, 실제 표본 간격 기록) 뒤 `scripts/lab.sh run L8` — 4 passed, 5분 0초. `run-20261025-215328.log`, `results-20261025-215328.jsonl`, `issue65-run3.console`.
  - **4회차**: `scripts/lab.sh run L8d` — 1 passed, 1분 50초. `results-20261025-215910.jsonl`, `issue65-run4.console`.
  - 모든 실행이 끝난 뒤 `docker ps -a`·`docker volume ls`·`docker network ls --filter label=io.itda.dso-lab=1` 이 비었다.
- 표본: 헬스 `?strict=1` 를 약 1초마다(본문과 HTTP 코드), 앱 컨테이너의 `/proc` 를 약 3.3초마다(`lab_tools/procs.py`, 이미지에 `ps` 가 없다).

## 실행 전에 고정한 기대 (랩 가정)

단절 시작 0초 기준. 근거는 코드다: 갱신 스레드 하나가 별칭을 순서대로 조회하고 `REFRESH` 를 쉰다. 관측 시각은 조회가 **끝난 뒤** 찍힌다. age > `REFRESH × 3` 이면 `stale`, 그 안에서 조회가 실패했으면 `remote_error`. 조회는 30초 뒤 프로세스 그룹 SIGKILL 로 끝난다.

| 항목 | 경계 | 성격 |
|---|---|---|
| `stale` 이 처음 보임 | ≤ 10초 (`REFRESH × 3` + 진행 중 조회 여유 `REFRESH` + 표본 간격 + 1) | 랩 가정(L8a 와 같음) |
| 첫 `remote_error` | 29–36초 (하한: 정상 조회 1초 미만 가정, 상한: 2 + 2 + 30 + 1 + 1) | 랩 가정 |
| `remote_error` 구간 시작 사이 | 32 ± 2초 (`REFRESH` + 30) | 코드상 도출 + 표본 여유 |
| 90초 안 `remote_error` 구간 | 2개 이상 | 코드상 도출 |
| strict HTTP | `unknown` 은 503, `caught_up` 은 200 | 코드상 확인 |
| `litestream ltx` 프로세스 | 동시 1개 이하, 좀비 없음, 한 PID ≤ 40초(30 + 거두기 5 + 표본 5), 복구 뒤 단절 중 PID 없음 | 코드상 도출 |
| `ltx` 검출기 동작(3회차부터) | 단절 중 `ltx` 를 본 표본 ≥ 3, 서로 다른 PID ≥ 2(빈 표본만으로 통과하지 않게) | 코드상 도출(90초에 30초 조회 2~3번) |
| 헬스 표본 간격(3회차부터) | 실제 최대 간격을 시각 판정의 표본 여유로 쓰고, 1.5초를 넘으면 '표본이 거칠다'로 판정 실패와 따로 보고 | 하네스 |
| 복구 뒤 `caught_up` | ≤ 120초 | 랩 가정(L8a 와 같음) |
| 쓰기 오류 | 0 | — |

이 표의 시간은 **보장 상한이 아니다.** 헬스는 조회 하나에 30초 타임아웃을 두지만 그 앞의 `litestream version`(10초)과 거두기 여유(5초)가 더해질 수 있고, `stale` 전환은 단절 직전 조회가 얼마나 걸렸는지에 달려 있다.

## 결과

모든 실행이 기대대로였다(판정 함수 `_checks.long_outage_problems`·`ltx_process_problems` 의 문제 0건, 하네스 문제 0건). 아래 L8c 는 3회차, 괄호는 2·1회차다. L8d 는 3회차, 괄호는 4회차이고 모두 양방향 무응답이다.

| | L8c 연결 거부(`s3` 끔) | L8d 양방향 무응답(`timeout` toxic 0, `downstream`+`upstream`) |
|---|---|---|
| 헬스 표본 최대 간격 | 1.1초 | 1.1초 (1.1) |
| 단절 전 | `caught_up`, strict 200 | `caught_up`, strict 200 |
| 첫 `stale` | 5.0초 (5.1, 5.1) | 5.1초 (5.0) |
| `remote_error` 구간 시작 | 31.4, 63.8초 (31.5·63.9, 31.6·63.0) | 31.3, 63.7초 (31.4, 63.8) |
| 구간 사이 | 32.4초 (32.4, 31.4) | 32.4초 (32.4) |
| `remote_error` 구간 길이(표본) | 약 6초 → 다시 `stale` | 같음 |
| 단절 중 상태 | `unknown`(`stale`/`remote_error`)만 | 같음 |
| 단절 중 strict | 모두 503 | 모두 503 |
| `ltx` 를 본 표본 / 동시 최대 / 서로 다른 PID | 27 / 1 / 3 | 26 / 1 / 3 (26 / 1 / 3) |
| 한 PID 가 처음·마지막으로 보인 사이 | 최대 28.3초 (28.4). 표본 간격은 약 3.3초이고 30초 타임아웃 안이다. 마지막 PID 는 단절 끝에서 잘렸다 | 최대 25.2초 (25.2) |
| 좀비 | 없음 | 없음 |
| 복구 뒤 `ltx` | 3번 표본 모두 0개 | 같음 |
| 복구 → `caught_up` | 7.1초 (3.1, 3.2) | 2.0초 (2.0) |
| 복구 뒤 strict | 200 | 200 |
| 쓰기 성공 / 오류 | 489 / 0 (471, 465 / 0) | 474 / 0 (464 / 0) |
| 복구 뒤 복제본 | 509행 = 최종 509행, integrity ok (491, 485 일치) | 494 = 494, ok (484 = 484) |

시계열(3회차 L8c, 상태가 바뀐 표본만): `0.0 caught_up(age 1.11, 200)` → `5.0 stale(6.15, 503)` → `31.4 remote_error(0.43)` → `37.5 stale(6.51)` → `63.8 remote_error(0.76)` → `69.8 stale(6.82)` → 단절 끝까지 `stale`. 복구 뒤에는 진행 중이던 조회가 타임아웃까지 매달려 `stale` 5표본, 그 조회가 끝나 `remote_error` 2표본이 나왔다. 7.1초에 `caught_up(local_ahead_within_grace)`. 복구 시간 3.1·3.2·7.1초의 차이는 복구 순간에 진행 중이던 조회가 얼마나 남았는지에 달렸다(최대 약 30초 + `REFRESH`).

- **`stale` → `remote_error` 전이는 일어나지만 한 번으로 끝나지 않는다.** 조회가 타임아웃으로 끝나면 `remote_error` 가 약 6초(age ≤ `REFRESH × 3`) 보인다. 다음 조회가 다시 30초 매달리는 동안 age 가 문턱을 넘어 `stale` 로 돌아간다. 단절 동안 `stale` 약 26초와 `remote_error` 약 6초가 약 32초 주기로 번갈아 나온다. 둘 다 `unknown` 이라 strict 는 계속 503 이다. `REFRESH` 가 15초(기본)면 약 45초 주기 가운데 `remote_error` 가 15초, 나머지 30초가 `stale` 일 것으로 계산된다(재지 않음).
- **연결 거부도 양방향 무응답과 똑같이 30초 매달린다.** 첫 `remote_error` 가 31초대이고, L8c 와 L8d 의 시각이 거의 같다. CLAUDE.md 함정 '연결 거부에도 `ltx` 는 재시도하며 매달린다'를 헬스 경로에서 다시 확인했다.
- **매달린 `ltx` 는 남지 않는다.** 30초마다 PID 가 바뀌고(타임아웃 → SIGKILL → 다음 조회), 동시에 둘이 보인 적이 없으며, 복구 뒤에는 0개다.
- **양방향 무응답에서도 복구가 빨랐다.** toxic 을 지운 뒤 2.0초(두 실행 모두)에 `caught_up` 이었고, 복제본은 최종 행 수와 같았다. Litestream 의 업로드 연결이 90초 동안 응답 없이 막혔다가 풀린 뒤에도 오래 매달리지 않았다.

### Litestream 로그

3회차부터는 단절 구간의 로그를 복구 **전에** 읽는다(1·2회차는 복구 뒤에 읽어, L8d 에 복구 시점의 INFO 한 줄이 섞였다).

| 구간 | 모든 Litestream 줄(`level=`) L8c / L8d(3회차, 4회차) | WARN | ERROR |
|---|---|---|---|
| 부팅 ~ 단절 직전 | 9 / 10, 9 | 0 | 0 |
| 90초 단절 | 0 / 0, 0 | 0 | 0 |
| 복구 뒤 ~ 종료 전 | 1 / 1, 1 (모두 `compaction complete` INFO) | 0 | 0 |

부팅 줄은 읽혔다(`litestream version=0.5.17`, `initialized db`, `replicating to ... endpoint=http://toxiproxy:18333`, `starting compaction monitor` …). 그러니 0줄은 '로그를 못 읽음'이 아니다. **연결 거부든 양방향 무응답이든, 90초 단절 동안 Litestream 은 아무 줄도 남기지 않았다.** 업로드 실패를 알리는 WARN/ERROR 도 없었다. D3 와 같은 결론을 분 단위 단절에서 확인한 것이다. 단절을 알린 로그는 헬스의 `health 'default': remote lookup failed: litestream ltx timed out after 30s` 한 줄뿐이었다(같은 메시지는 반복하지 않는다).

## amd64 실행 (GitHub Actions)

`.github/workflows/lab.yml`(ubuntu-latest, linux/amd64)을 `-f scenarios=L8` 로 돌렸다. 실행은 오케스트레이터가 했고, artifact 는 `gh run download` 로 받아 읽었다.

- **run 40196113088**(2026-10-25 13:06Z, 커밋 `b259463` = 양방향 L8d): L8a·L8b·L8c·L8d 4 passed, 292초. 판정 문제·하네스 문제 0건.
- run 40194194089(커밋 `500384e`, 리뷰 1 반영 전, L8d 는 `downstream` toxic 만): 4 passed. 응답 유실 실행이라 아래 표에는 넣지 않았다.

| | L8c 연결 거부 | L8d 양방향 무응답 |
|---|---|---|
| 헬스 표본 최대 간격 | 1.1초 | 1.1초 |
| 첫 `stale` | 5.0초 | 5.0초 |
| `remote_error` 구간 시작 | 31.1, 63.2초 | 31.1, 63.2초 |
| `ltx` 를 본 표본 / 동시 최대 / 서로 다른 PID / 복구 뒤 | 26 / 1 / 3 / 0 | 26 / 1 / 3 / 0 |
| 쓰기 성공 / 오류 | 513 / 0 | 484 / 0 |
| 복구 → `caught_up` | 7.0초 | 1.0초 |
| 복구 뒤 복제본 | 533 = 533, integrity ok | 504 = 504, ok |
| 단절 중 Litestream 줄 | 0 | 0 |

arm64(colima)와 같은 모양이다. 첫 `remote_error` 는 31초대이고, 구간 간격은 약 32초다. 러너에서도 표본 간격이 1.1초라 경계(32 ± 2초)까지 여유가 있었다.

## 여러 DB 별칭 (코드상 확인 + 주입 시계 재현)

랩 앱은 별칭 하나뿐이라 랩에서는 재지 않았다. `Monitor._run` 은 스레드 하나가 `refresh_once()` 에서 모든 별칭을 순서대로 조회한다. 그래서 한 별칭의 조회가 30초 매달리면 정상 별칭의 관측 간격도 `30 + REFRESH`(+ 다른 조회 시간)로 늘어난다. 그 값이 `REFRESH × 3` 을 넘으면 — 즉 매달린 시간이 `2 × REFRESH` 를 넘으면 — **정상 별칭도 `stale`(unknown)** 이 된다.

- `REFRESH` 2초(랩): 매 주기 정상 별칭도 `stale`. 축척 실험(REFRESH 0.5초, 한 별칭 조회 3초, 실제 `Monitor` 스레드)에서 정상 별칭이 표본 32개 중 15개 `stale` 이었다.
- `REFRESH` 15초(기본): 30 + 15 = 45 = 문턱이라, 정상 별칭의 나이가 45초를 **다른 조회 시간만큼** 넘는 짧은 동안 `stale`. 매달린 별칭이 둘이면 60 + 15 > 45 라 길어진다.
- 단언: `tests/test_health.py::test_monitor_one_hanging_alias_delays_the_others`(주입 monotonic, REFRESH 2·15).

## 미검증

- 여러 별칭을 가진 실제 컨테이너(랩 확장 필요), `REFRESH` 기본값(15초)에서의 실제 시계열.
- 분 단위보다 긴 단절(수십 분~시간), 단절 중 Litestream 프로세스의 메모리·L0 누적.
- 다른 S3 구현·실제 클라우드 S3 에서 연결 거부 시 `ltx` 가 같은 방식으로 매달리는지.
