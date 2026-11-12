# Litestream 0.5.18 검증 (2026-11-12, #99)

Litestream v0.5.18(2026-11-03 릴리스)을 boot 의 버전 게이트에 넣기 전에 잰 것이다. 대상은 실측 fixture, 단위·통합 테스트, 회귀 랩(L1–L8, P1·P2)이다. 수치는 모두 아래 실행에서 나온 값이다. 결론: 출력 **형식**은 0.5.17 과 같았다. **S3 가 연결을 거부할 때의 동작**이 다르다. 0.5.18 은 매달리지 않고 약 7초 뒤 rc 1 로 끝나며 ERROR 로그를 남긴다. 판정 결과(`unknown`, exit 2)는 두 판에서 같아서 패키지 코드는 바꾸지 않았고, 랩 L4·L8c 의 기대값을 판별로 바꿨다.

## 환경

- 호스트: macOS(arm64), colima(linux/arm64). 같은 VM 에서 다른 프로젝트 컨테이너가 돌았다(부하는 통제하지 않았다). Docker 29.5.2.
- fixture·pytest: 릴리스 `litestream-0.5.18-darwin-arm64` 바이너리(같은 릴리스 `checksums.txt` 와 SHA-256 일치)를 `PATH` 앞에 두거나 `LITESTREAM=` 로 썼다. 대조는 brew 의 0.5.17 이다.
- 랩 이미지: [`single-server` 문서 Dockerfile](../profiles/single-server.md#dockerfile) 그대로, `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`, Debian SQLite 3.46.1, Python 3.13.18. Litestream 은 릴리스 `.deb`(체크섬 확인)다.
  - 0.5.18: `dso-lab-app:bea4f74-ls0.5.18`.
  - 0.5.17: `LAB_LITESTREAM_VERSION=0.5.17` → `dso-lab-app:bea4f74-ls0.5.17`.
  - 각 실행이 이미지 안 `litestream version` 을 읽어 로그(`[lab] image litestream version: …`)와 결과 줄(`litestream_version`)에 남겼다.
- S3·장애 주입은 [`lab/README.md`](../../lab/README.md) 그대로다(SeaweedFS 4.48, toxiproxy 2.12.0).

## 1. fixture (`tests/fixtures/litestream-0.5.18/`)

`tests/fixtures/make_litestream_fixtures.sh 0.5.18 …` 로 만들었다. 0.5.17 과 폴더·파일 집합(60개)이 같다.

- **모든 폴더의 `rc`·`stderr` 가 바이트 단위로 같다.**
- `stdout` 이 다른 곳은 값뿐이다.
  - `version` 의 판.
  - `ltx_all_levels`·`ltx_no_l0_all_levels`·`ltx_l0_default` 의 `timestamp`·`size`, 그리고 L2·L9 압축 묶음(실행 시각에 따른 경계). 키 집합은 같고 최대 TXID 는 둘 다 `0x19` 다.
  - `restore_ok_json` 의 로그 줄 시각과 `duration_ms`. 로그 줄 형식과 JSON 키는 같다.
  - LTX 헤더의 시각·salt·체크섬.
- 자세한 표는 [fixture README](../../tests/fixtures/litestream-0.5.18/README.md#0517-과의-차이-2026-11-12-99) 다.

fixture 는 `file://` 복제본만 쓴다. S3 경로의 차이는 아래 랩에서 나왔다.

## 2. pytest

`REQUIRE_LITESTREAM=1 uv run pytest -q -rs`, 게이트에 0.5.18 을 넣은 뒤:

| PATH 의 litestream | 결과 |
|---|---|
| 0.5.17 | 3632 passed, 2 skipped |
| 0.5.18 | 3632 passed, 2 skipped |

skip 2개는 macOS 의 대소문자 무시 볼륨 전용 테스트다. 게이트를 바꾸기 전에는 새 판별 테스트가 실패했다(`test_boot_litestream.py` 18개, 0.5.18 PATH 의 실제 바이너리 테스트 6개).

## 3. 회귀 랩

### 실행 목록

| 실행 | 이미지 안 판 | 결과 | 결과 파일(git 무시) |
|---|---|---|---|
| `run all`(옛 0.5.17 전용 기대값) | 0.5.18 | 12 passed, **L8c 실패** | `results-20261112-130454.jsonl` |
| `run L8c` × 3(판별 기대값) | 0.5.18 | 1 passed × 3 | `results-20261112-133336`·`133556`·`133819.jsonl` |
| `run all`(판별 기대값) | 0.5.18 | **13 passed** | `results-20261112-134113.jsonl` |
| `LAB_LITESTREAM_VERSION=0.5.17 run "L4 or L8c or L8d"` | 0.5.17 | **3 passed** | `results-20261112-135551.jsonl` |

정리 뒤 `docker ps -a`·`docker volume ls --filter label=io.itda.dso-lab=1` 은 매번 비었다.

### L4 — 부팅 중 S3 끊김

| 판 | 연결 거부: 거부까지 · 사유 | 무응답(`--ltx-timeout 5`): 거부까지 · 사유 |
|---|---|---|
| 0.5.17 | 30.6초 · `timed out` | 5.6초 · `timed out` |
| 0.5.18 | 7.7초(옛 run all), 8.0초 · `exited with 1` | 5.9·5.7초 · `timed out` |

0.5.18 연결 거부 때 boot 의 사유 줄:

```
remote lookup failed: litestream ltx exited with 1: Error: operation error S3: ListObjectsV2, exceeded maximum number of attempts, 8, https response error StatusCode: 0, RequestID: , HostID: , request send failed, Get "http://toxiproxy:18333/dso-lab?list-type=2&prefix=…": dial tcp …
```

두 판 모두 exit 2 `remote_error` 이고, 복구 뒤에는 `match` 로 진행했다. 랩은 이제 사유의 종류만 판별로 단언하고 시간은 기록만 한다.

### L8c — S3 연결 거부 90초 (REFRESH 2초)

| 실행 | 판 | 첫 `stale` | `remote_error` 구간 시작(초) | 구간 간격 | 조회 실패 사유 | 단절 중 Litestream ERROR | `ltx` PID | 복구 |
|---|---|---|---|---|---|---|---|---|
| run all(옛 기대) | 0.5.18 | 5.1 | 8.1, 17.2, 26.3, … 89.3 (10) | 8.1–9.2 | `exited with 1` | 7 | 10 | 1.0초 |
| L8c r1 | 0.5.18 | 5.1 | 8.1, 17.2, … 81.0 (9) | 9.1–9.2 | `exited with 1` | 7 | 10 | 2.0초 |
| L8c r2 | 0.5.18 | 5.1 | 8.1, 17.2, … 81.0 (9) | 9.1–9.2 | `exited with 1` | 7 | 10 | 2.0초 |
| L8c r3 | 0.5.18 | 5.1 | 8.1, 17.2, … 81.0 (9) | 9.1–9.2 | `exited with 1` | 7 | 10 | 2.0초 |
| run all(판별) | 0.5.18 | 5.1 | 8.1, 17.2, … 81.1 (9) | 9.1–9.2 | `exited with 1` | 7 | 10 | 2.0초 |
| 대조 | 0.5.17 | 5.1 | 31.5, 63.0 (2) | 31.5 | `timed out` | 0 | 3 | 7.1초 |

- 두 판 모두 단절 내내 `unknown`·strict 503 이었다. 쓰기 오류 0, 복구 뒤 `caught_up`·200, 복제본 행 수 = 최종 행 수였다. `ltx` 는 동시에 1개 이하이고 좀비는 없었다.
- 0.5.18 의 조회 하나는 간격 − REFRESH ≈ **6.1–7.2초**다. 이 시간은 Litestream(AWS SDK)의 S3 재시도에서 나오는 잰 값이지 설정값이 아니다.
- 경계(`lab/_checks.py` `REFUSED_LOOKUP["0.5.18"]`): 조회 3–15초로 크게 여유를 뒀다. 첫 `remote_error` 2–21초, 구간 간격 11±8초(표본 간격 1초 기준), 4구간 이상이다. 0.5.17 의 30초 매달림과는 겹치지 않는다. 단위 테스트(`tests/test_lab.py`)가 "0.5.18 시계열은 0.5.17 경계에서 실패, 그 반대도 실패"를 본다.
- 판정 순서: 단절 중 헬스의 조회 실패 줄(`health 'default': remote lookup failed: …`)이 모두 그 판의 종류인지 먼저 본 뒤 시간을 본다. 이 줄은 단절마다 한 번 나왔다(전이 때만 로그).

0.5.18 의 단절 중 Litestream ERROR 줄(판별 run all, 문구 그대로, 300자에서 자름):

```
time=2026-11-12T04:51:40.592Z level=ERROR msg="monitor error" system=store db=app.sqlite3 replica=s3 error="write ltx file: s3: put object l8c-1112134113/0000/0000000000000004-0000000000000004.ltx: operation error S3: PutObject, exceeded maximum number of attempts, 8, https response error StatusCode
time=2026-11-12T04:51:51.582Z level=ERROR msg="l0 retention enforcement failed" system=store db=app.sqlite3 path=/data/app.sqlite3 error="close l1 iterator: operation error S3: ListObjectsV2, exceeded maximum number of attempts, 8, https response error StatusCode: 0, RequestID: , HostID: , request s
time=2026-11-12T04:52:23.702Z level=ERROR msg="monitor error" system=store db=app.sqlite3 replica=s3 error="calc pos: max ltx file: operation error S3: ListObjectsV2, exceeded maximum number of attempts, 8, https response error StatusCode: 0, RequestID: , HostID: , request send failed, Get \"http://
```

7줄 가운데 `l0 retention enforcement failed` 가 5줄이고, `monitor error` 두 종류가 1줄씩이다(모든 0.5.18 L8c 실행에서 7줄). 0.5.17 은 같은 단절에서 0줄이었다(D3).

### L8d — 양방향 무응답 90초

| 판 | `remote_error` 구간 시작(초) | 사유 | 단절 중 Litestream 로그 |
|---|---|---|---|
| 0.5.18(옛 run all / 판별 run all) | 31.4, 62.8 / 31.4, 63.9 | `timed out after 30s` | 0줄 |
| 0.5.17 | 31.4, 63.9 | `timed out after 30s` | 0줄 |

무응답에는 0.5.18 도 30초 타임아웃까지 매달린다. 기대는 두 판 모두 0.5.17 의 것 그대로다.

### 나머지 시나리오 (0.5.18)

P1·P2(채널 시나리오 포함), L1, L2, L3, L5, L6(kill 지점 전부), L7, L8a, L8b 는 0.5.17 때와 같은 단언으로 통과했다(옛·판별 두 `run all` 모두).

### amd64 실행 (GitHub Actions)

`lab.yml` run `43159688920`, 브랜치 `issue-99-litestream-0518`(커밋 `22e613e`), ubuntu-latest amd64, `scenarios=all`. 오케스트레이터가 실행했다(재현함).

- 결과: `13 passed, 5 deselected in 808.16s`. 이미지 안 판 로그는 `image litestream version: 0.5.18` 이고, 결과 줄 13개 모두 `litestream_version` 이 0.5.18 이다.
- L4: 연결 거부 7.7초 `exit1`(exit 2), 무응답 5.6초 `timeout`(exit 2).
- L8c(연결 거부 90초)
  - `remote_error` 구간 시작 8.0·17.0·26.1·35.1·44.1·53.1·63.2·72.2·81.2초(9개, 간격 9.0–10.1초).
  - 조회 실패 종류 `exit1`. 단절 중 Litestream 로그 7줄.
  - 복구 1.0초 뒤 `caught_up`. 쓰기 484건·오류 0. 복제본 504행·integrity ok.
  - `ltx` 는 동시에 1개 이하, 서로 다른 PID 10개.
- L8d(양방향 무응답 90초)
  - `remote_error` 구간 시작 31.1·63.1초. 종류 `timeout`. 단절 중 Litestream 로그 0줄.
  - 복구 2.0초 뒤 `caught_up`. 쓰기 489건·오류 0. 복제본 509행·integrity ok.
- arm64 와 같은 꼴이다(첫 `remote_error` 8.0초 대 8.1초, 간격 9.0–10.1초 대 9.1–9.2초). 정한 경계(첫 2–21초, 간격 11±8초) 안이다.

## 판정과 남은 것

- 원인: 상류 0.5.18 의 어느 변경(#1528 LTX 스트림 읽기 시간 제한, #1572 복제 클라이언트 오류 로그 등) 때문인지는 **미검증**이다. 동작은 재현했다.
- 판정할 수 없는 출력은 나오지 않았다. 패키지의 파서·판정 코드는 그대로 맞았다(rc 1 + stderr → `RemoteError` → `unknown`·exit 2).
- 남은 것: 다른 함정의 0.5.18 재측정(VFS 확장 로드 #1506 등)은 #105 다.
