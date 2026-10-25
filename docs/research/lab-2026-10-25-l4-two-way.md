# 회귀 랩 실측: L4 양방향 무응답 (2026-10-25, #71)

`lab/` 의 L4(S3 끊김 중 부팅) 가운데 '무응답' 단계를 양방향으로 바꾸고 다시 돌렸다. 수치는 모두 아래 실행에서 나온 실제 값이다. 이전 L4 는 [`lab-2026-10-08.md`](lab-2026-10-08.md#l4-s3-끊김-중-부팅) 에 있다.

## 바꾼 것

- **이전(2026-10-08 실행, #10)의 무응답 단계는 `timeout` toxic 하나를 `stream` 없이 넣었다.** toxiproxy 2.12.0 README 에서 `stream` 은 "link direction to affect (defaults to `downstream`)" 이다. 그래서 요청(client → S3)은 SeaweedFS 에 닿고 응답(S3 → client)만 버려지는 '응답 유실'이었다. 이름·문서의 '무응답'과 달랐다(#71, #65 리뷰 1 P2-2 에서 L8d 의 같은 문제를 찾음).
- 이번에는 #65 의 L8d 와 같이 `timeout` toxic(`timeout` 0 = 연결은 닫지 않고 데이터를 버림)을 `hang_downstream`·`hang_upstream` 두 개로, `stream` 을 명시해 넣었다. 복구는 그대로 `POST /reset`(`reset_proxies()`). 결과 기록에 `hang_streams = ["downstream", "upstream"]` 을 남긴다.
- 연결 거부 단계(프록시 끔)와 판정(exit 2, `reason_code=remote_error`, DB 해시 그대로, 복구 뒤 `match`)은 바꾸지 않았다.

## 환경

- 호스트: macOS(arm64) + colima(linux/arm64). 같은 VM 에서 다른 프로젝트 컨테이너가 돌고 있었다(부하는 통제하지 않았다).
- Docker 29.5.2, Docker Compose 5.5.1. compose 프로젝트 `dso-lab-i71`.
- 앱 이미지: `single-server` 문서 Dockerfile 그대로(`python:3.13-slim` = `sha256:bf44cdfc…`, Litestream 0.5.17). 패키지는 작업 트리 wheel(커밋 `eba814a` 위 작업 트리, 제품 코드 변경 없음).
- S3: SeaweedFS 4.48, 장애 주입: toxiproxy 2.12.0(랩 README 의 digest 고정 그대로).
- 실행: `LAB_PROJECT=dso-lab-i71 scripts/lab.sh run L4` 두 번, 둘 다 1 passed.
  - 1회차: `lab/.out/run-20261025-231616.log`, `results-20261025-231617.jsonl`(테스트 45.9초).
  - 2회차: `run-20261025-231756.log`, `results-20261025-231756.jsonl`(테스트 43.6초).
  - 끝난 뒤 `docker ps -a`·`docker volume ls`·`docker network ls --filter label=io.itda.dso-lab=1` 이 비었다.

## 결과

| 단계 | 2026-10-08(#10) | 2026-10-25 1회차 | 2026-10-25 2회차 |
|---|---|---|---|
| 연결 거부(프록시 끔), 기본 `--ltx-timeout` 30 | exit 2 `remote_error`, 30.5초 | exit 2 `remote_error`, 30.6초 | exit 2 `remote_error`, 30.6초 |
| 무응답 + `--ltx-timeout 5` | `downstream` 만(응답 유실): exit 2 `remote_error`, 5.5초 | **양방향**: exit 2 `remote_error`, 5.6초 | **양방향**: exit 2 `remote_error`, 5.6초 |
| 복구 뒤 같은 볼륨 | `match`, DB 해시 그대로 | `match`, 20건, DB 해시 그대로 | 같음 |

양방향 무응답의 boot 줄(두 회차 같음):

```
[boot] inputs: local_exists=True local_empty=False local_txid=2 remote=error: litestream ltx timed out after 5s
[boot] decision: state=unknown action=refuse reason_code=remote_error reason=local db exists, local txid 2; remote lookup failed: litestream ltx timed out after 5s
[boot] exit 2: refused (remote_error): local db exists, local txid 2; remote lookup failed: litestream ltx timed out after 5s
```

## 결론

- 양방향 무응답에서도 boot 는 `--ltx-timeout`(5초) 뒤 `remote_error` 로 거부한다(재현함, 2회). 응답 유실만이던 이전 값(5.5초)과 판정·시간이 같다. 이슈에서 '영향 없을 것(미검증)'이라 본 것이 이 실행으로 확인됐다.
- 이 결과는 arm64(colima)에서만이다. GitHub 러너(amd64, `lab.yml -f scenarios=L4`)는 별도로 돈다.
