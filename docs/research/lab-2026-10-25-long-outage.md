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
  - **2회차(대표)**: 로그 요약에 모든 Litestream 줄 수를 더한 뒤 `scripts/lab.sh run "L8c or L8d"` — 2 passed, 3분 39초. `run-20261025-212318.log`, `results-20261025-212318.jsonl`, `issue65-run2.console`.
  - 두 실행 모두 끝난 뒤 `docker ps -a`·`docker volume ls`·`docker network ls --filter label=io.itda.dso-lab=1` 이 비었다.
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
| 복구 뒤 `caught_up` | ≤ 120초 | 랩 가정(L8a 와 같음) |
| 쓰기 오류 | 0 | — |

이 표의 시간은 **보장 상한이 아니다.** 헬스는 조회 하나에 30초 타임아웃을 두지만 그 앞의 `litestream version`(10초)과 거두기 여유(5초)가 더해질 수 있고, `stale` 전환은 단절 직전 조회가 얼마나 걸렸는지에 달려 있다.

## 결과

두 실행 모두 기대대로였다(판정 함수 `_checks.long_outage_problems`·`ltx_process_problems` 의 문제 0건). 아래는 2회차, 괄호는 1회차.

| | L8c 연결 거부(`s3` 끔) | L8d 무응답(`timeout` toxic, 0) |
|---|---|---|
| 단절 전 | `caught_up`, strict 200 | `caught_up`, strict 200 |
| 첫 `stale` | 5.1초 (5.1) | 5.1초 (5.1) |
| `remote_error` 구간 시작 | 31.5, 63.9초 (31.6, 63.0) | 31.3, 63.7초 (31.4, 64.0) |
| 구간 사이 | 32.4초 (31.4) | 32.4초 (32.6) |
| `remote_error` 구간 길이(표본) | 약 6초 → 다시 `stale` | 약 6초 → 다시 `stale` |
| 단절 중 상태 | `unknown`(`stale`/`remote_error`)만 | 같음 |
| 단절 중 strict | 모두 503 | 모두 503 |
| `ltx` 동시 최대 / 서로 다른 PID | 1 / 3 (1 / 3) | 1 / 3 (1 / 3) |
| 한 PID 가 처음·마지막으로 보인 사이 | 최대 28.4초(표본 약 3.3초 간격, 30초 타임아웃 안. 마지막 PID 는 단절 끝에서 잘림) | 최대 25.4초 |
| 좀비 | 없음 | 없음 |
| 복구 뒤 `ltx` | 3번 표본 모두 0개 | 같음 |
| 복구 → `caught_up` | 3.1초 (3.2) | 1.1초 (1.2) |
| 복구 뒤 strict | 200 | 200 |
| 쓰기 성공 / 오류 | 471 / 0 (465 / 0) | 469 / 0 (457 / 0) |
| 복구 뒤 복제본 | 491행 = 최종 491행, integrity ok (485 = 485) | 489 = 489, ok (477 = 477) |

시계열(2회차 L8c, 상태가 바뀐 표본만): `0.0 caught_up(age 1.18, 200)` → `5.1 stale(6.29, 503)` → `31.5 remote_error(0.68)` → `37.6 stale(6.78)` → `63.9 remote_error(1.01)` → `69.0 stale(6.07)` → 단절 끝 89.7초 `stale(26.9)`. 복구 뒤 `stale` 3표본(진행 중이던 조회가 타임아웃까지 매달림) → 3.1초 `caught_up(local_ahead_within_grace)`.

- **`stale` → `remote_error` 전이는 일어나지만 한 번으로 끝나지 않는다.** 조회가 타임아웃으로 끝나면 `remote_error` 가 약 6초(age ≤ `REFRESH × 3`) 보이고, 다음 조회가 다시 30초 매달리는 동안 age 가 문턱을 넘어 `stale` 로 돌아간다. 단절 동안 `stale` 약 26초 / `remote_error` 약 6초가 약 32초 주기로 번갈아 나온다. 둘 다 `unknown` 이라 strict 는 계속 503 이다. `REFRESH` 15초(기본)면 `remote_error` 가 약 45초 중 15초, 나머지 30초는 `stale` 이 될 것으로 계산된다(재지 않음).
- **연결 거부도 무응답과 똑같이 30초 매달린다.** 첫 `remote_error` 가 31초대이고 L8c·L8d 의 시각이 거의 같다(CLAUDE.md 함정 '연결 거부에도 `ltx` 는 재시도하며 매달린다'의 재확인, 헬스 경로에서).
- **매달린 `ltx` 는 남지 않는다.** 30초마다 PID 가 바뀌고(타임아웃 → SIGKILL → 다음 조회), 동시에 둘이 보인 적이 없으며 복구 뒤 0개다.
- **복구는 빠르다.** 무응답형도 Litestream 의 기존 연결이 오래 매달리지 않았다: 복구 1.1초 뒤 `caught_up`.

### Litestream 로그

| 구간 | 모든 Litestream 줄(`level=`) | WARN | ERROR |
|---|---|---|---|
| 부팅 ~ 단절 직전 | 9 (L8c) / 9 (L8d) | 0 | 0 |
| 90초 단절 (+ 복구 직후 로그를 읽기까지) | 0 / 1 | 0 | 0 |
| 복구 뒤 ~ 종료 전 | 0 / 1 | 0 | 0 |

부팅 줄(`litestream version=0.5.17`, `initialized db`, `replicating to ... endpoint=http://toxiproxy:18333`, `starting compaction monitor` …)은 읽혔으므로 0줄은 '로그를 못 읽음'이 아니다. L8d 의 두 줄은 `compaction complete`(INFO)로, 단절 구간의 로그는 복구 **뒤에** 읽으므로 그 한 줄(12:26:50.8)은 복구 시점 언저리의 것이다. 즉 **90초 단절 동안 Litestream 은 업로드 실패를 WARN/ERROR 로 한 줄도 남기지 않았다**(D3 와 같은 결론을 분 단위 단절로 확인). 단절을 알린 유일한 로그는 헬스의 `health 'default': remote lookup failed: litestream ltx timed out after 30s`(같은 메시지는 반복하지 않음) 한 줄이었다.

## 여러 DB 별칭 (코드상 확인 + 주입 시계 재현)

랩 앱은 별칭 하나뿐이라 랩에서는 재지 않았다. `Monitor._run` 은 스레드 하나가 `refresh_once()` 에서 모든 별칭을 순서대로 조회한다. 그래서 한 별칭의 조회가 30초 매달리면 정상 별칭의 관측 간격도 `30 + REFRESH`(+ 다른 조회 시간)로 늘어난다. 그 값이 `REFRESH × 3` 을 넘으면 — 즉 매달린 시간이 `2 × REFRESH` 를 넘으면 — **정상 별칭도 `stale`(unknown)** 이 된다.

- `REFRESH` 2초(랩): 매 주기 정상 별칭도 `stale`. 축척 실험(REFRESH 0.5초, 한 별칭 조회 3초, 실제 `Monitor` 스레드)에서 정상 별칭이 표본 32개 중 15개 `stale` 이었다.
- `REFRESH` 15초(기본): 30 + 15 = 45 = 문턱이라, 정상 별칭의 나이가 45초를 **다른 조회 시간만큼** 넘는 짧은 동안 `stale`. 매달린 별칭이 둘이면 60 + 15 > 45 라 길어진다.
- 단언: `tests/test_health.py::test_monitor_one_hanging_alias_delays_the_others`(주입 monotonic, REFRESH 2·15).

## 미검증

- 여러 별칭을 가진 실제 컨테이너(랩 확장 필요), `REFRESH` 기본값(15초)에서의 실제 시계열.
- GitHub Actions amd64 러너에서의 L8c·L8d(`gh workflow run lab.yml -f scenarios=L8`).
- 분 단위보다 긴 단절(수십 분~시간), 단절 중 Litestream 프로세스의 메모리·L0 누적.
- 다른 S3 구현·실제 클라우드 S3 에서 연결 거부 시 `ltx` 가 같은 방식으로 매달리는지.
