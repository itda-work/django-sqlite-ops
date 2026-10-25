# 랩 P2: multiproc 워커 사이 channels-nats 전달 (2026-10-26, #66)

`single-server-multiproc` 문서의 compose·entrypoint·settings 그대로에 channels-nats 를 넣어, **서로 다른 uvicorn 워커** 사이 채널 레이어 전달·중복·손실·종료를 잰다. channels-nats D2 의 196/200 이 하네스 문제인지도 판정한다. channels-nats 는 고치지 않았다(외부 저장소는 읽기만 했다).

## 0. 환경

| 항목 | 값 |
|---|---|
| 호스트 | macOS, colima linux/arm64, Docker 29.5.2, Compose 5.5.1 |
| 앱 이미지 | 문서 Dockerfile(`python:3.13-slim@sha256:bf44cdfc…`) + 랩 층 `lab/nats.Dockerfile`: `django-sqlite-ops[nats] @ file:///app/<wheel>`, `channels-nats==0.10.0` |
| 판 | channels-nats 0.10.0(PyPI), channels 4.3.2, nats-py 2.16.0, msgpack 1.2.3, Django 6.1.2, uvicorn 0.54.0, nats-server 이미지 `nats:2.15.0@sha256:cd3fcd4e…` |
| 워커 | `WEB_WORKERS=2`(uvicorn `--workers 2`), 레이어 설정은 문서 그대로(`servers: ["nats://nats:4222"]`, 나머지 기본: `capacity` 100, `expiry` 60) |
| 대표 실행 | `scripts/lab.sh run P2`, 결과 `lab/.out/results-20261026-003015.jsonl`: `test_P2_multiproc_compose` passed(181초), `test_P2_nats_flush_cancel_known_defect` xfailed(strict, 58초) |

## 1. 기대값 (실행 전에 고정)

출처는 channels-nats 0.10.0 README 'Channels 규약과 다른 점'과 `channels_nats/layer.py`(로컬 사본 `v0.10.0-2-gb778ae1`, 0.10.0 과 코드 차이 없음).

| 근거 | 내용 |
|---|---|
| `new_channel()`·`group_add()` | 반환 전에 구독을 만들고 서버 확인(`flush`)을 기다린다 |
| 일반 채널 첫 `receive()` | 그때 큐 그룹 구독을 만들고 확인한다. 두 프로세스가 같은 이름을 읽으면 메시지마다 한쪽만 받는다 |
| 일반 채널 `receive()` 취소 | 마지막 수신자가 취소되면 mailbox 를 지우고 구독을 해지한다(`_discard_mailbox`). 두 폴링 사이에 온 것은 사라진다 — **계약상 허용 손실** |
| 프로세스 전용 채널(`!`)·그룹 | 취소해도 구독·mailbox 를 유지한다(유휴 `mailbox_grace` 60초, 그룹 소속이면 계속). 취소 공백 손실 없음 |
| `capacity`(100) | mailbox 가 차면 새 메시지를 버리고 `channels_nats` 로거에 경고한다. 보내는 쪽은 모른다(`ChannelFull` 없음) — **계약상 허용 손실** |
| 순서 | 구독 하나 안에서만 |
| 종료 | `asyncio.run()` 이 루프를 닫을 때 레이어가 연결을 닫는다(`_release_on_close`). `close()` 는 유계 |

수신 준비 확인: 두 워커가 수신 루프를 띄운 것을 HTTP 로 확인(서로 다른 PID 둘)하고, **probe 를 두 워커가 모두 1건 이상 받을 때까지** 보낸 뒤 본 메시지를 보낸다. probe 는 집계에서 뺀다.

## 2. 하네스

| 조각 | 하는 일 |
|---|---|
| `lab/app/notes/chanlab.py` | 워커의 비동기 뷰. `listen`(이 워커에 수신 루프 — 그룹·전용·일반, 계속/`wait_for` 취소), `state`·`stop`, `send`(워커 → 워커), `tasks`(진단: 워커 루프의 asyncio 태스크 스택). `channels_nats`·`nats` 로거 기록을 프로세스 안에 모은다 |
| `lab/app/lab_tools/chanlab_run.py` | 앱 컨테이너 안의 별도 프로세스. 두 워커에 수신을 띄우고 → probe 로 준비 확인 → 본 메시지 200건 → 수신이 멈출 때까지 대기 → 꼬리 probe → 다시 대기 → `stop` → `await layer.close()`(또는 닫지 않음) |
| `lab/_checks.py` | `delivery_summary`·`delivery_problems`·`nats_drops`(순수 함수, `tests/test_lab.py` 가 Docker 없이 검사) |

꼬리 probe 를 두는 이유: 레이어는 드롭 합계("has room again after dropping N")를 **자리가 난 뒤 다음 메시지가 들어올 때** 남긴다. 버스트 뒤 메시지가 없으면 첫 경고("1 message(s) dropped so far")만 남는다. 꼬리 probe 없이 돈 보조 실행에서 BURST 의 손실 8 이 로그 1 로 보였다.

## 3. 결과 (대표 실행)

N = 200, 수신 워커 2개. 간격은 송신 사이 `asyncio.sleep`.

| ID | 채널·수신 | 송신 | 결과 | 기대와 |
|---|---|---|---|---|
| G-steady | 그룹, 계속 수신 | 별도 프로세스 `group_send`, 2ms | 200/200 ×2, 중복 0, 순서 유지, 준비 0.011초(probe 1) | 일치 |
| G-cancel | 그룹, `wait_for(…, 1ms)` (워커당 취소 2,066·2,067회) | 같음 | 200/200 ×2, 손실 0 | 일치 |
| G-worker | 그룹, 계속 | **워커**의 비동기 뷰가 `group_send` | 200/200 ×2 | 일치 |
| S-steady | 프로세스 전용 채널 1:1 | 별도 프로세스 `send` | 200/200 ×2 | 일치 |
| S-cancel | 같음, 1ms 취소(1,736·1,747회) | 같음 | 200/200 ×2 | 일치 |
| S-worker | 같음 | **다른 워커**가 그 채널로 `send` | 200/200 ×2, 송신 PID ≠ 수신 PID | 일치 |
| P-steady | 일반 채널, 두 워커가 같은 이름(큐 그룹) | 별도 프로세스, 2ms | 합 200, 중복 0, 분배 102/98 | 일치 |
| P-cancel | 일반 채널, `wait_for(…, 10ms)`(293·290회), 별도 레이어 인스턴스 | 5ms | 89+95 = **184/200, 손실 16**, 중복 0, 용량 드롭 0 | 일치(취소 공백 손실 **재현함**) |
| P-cancel-after | 그룹, 위와 **같은 레이어 인스턴스** | 2ms | 200/200 ×2 | 일치(10ms 폴링 뒤 연결 정상) |
| BURST | 그룹, 계속 | 간격 0(0.001초에 200건) | 100+195 수신, 워커별 손실 100·5 = 로그의 드롭 100·5 | 일치(손실 = 용량 드롭) |
| NOCLOSE | 그룹 | 2ms, 송신 프로세스가 `close()` 없이 끝남 | 200/200 ×2, 프로세스 정상 종료(rc 0) | 일치 |
| LIVE + `docker stop` | 그룹 수신 루프를 두 워커에 남긴 채 | — | 200/200 ×2. stop exit 0, **0.74초**, 워커 둘 `Shutting down`·`Finished server process`, 트레이스백·`channels_nats` 줄 0, 복제 400/400 `integrity ok`, 재부팅 `match` | 일치 |

- 별도 송신 프로세스의 `await layer.close()` 는 0.000–0.002초였다(모든 시나리오 rc 0, 컨테이너 안 `timeout 300` 아래).
- doctor: `OK default backend channels_nats.NatsChannelLayer` (exit 0).
- 그룹·전용 채널은 1ms 취소를 수천 번 겪어도 손실이 없었다. 이미 있는 mailbox 를 읽는 `receive()` 는 구독 확인을 기다리지 않기 때문이다(§5 의 결함이 이쪽에서 나지 않는 이유, 코드상 확인).

## 4. D2 196/200 판정 — **재현함: 수신 쪽 mailbox 용량(100) 드롭. 하네스(버스트 > capacity)의 결과다**

D2 와 같은 꼴: 일반 채널 하나를 워커 둘이 `wait_for(receive, 0.5)` 로 읽고, 별도 프로세스가 200건을 간격 없이 보낸다(probe 로 준비 확인 뒤). 시도 20회. 수신 쪽 레이어만 `capacity` 를 바꾼 판(`cap1000`, 별도 인스턴스)과 비교한다.

| 실행 | `cap100`(문서 설정) | `cap1000` |
|---|---|---|
| 대표 실행 | 손실 시도 **13/20**, 손실 1–15건 | **0/20** |
| 보조 실행 4(꼬리 probe 전) | 9/20, 2–11건 | 0/20 |
| 보조 실행 5 | 10/20, 1–16건 | (돌리지 않음) |

- 손실이 난 시도 **32건 모두에서 한 워커가 정확히 100건**을 받았고 그 워커에 `mailbox … is full (capacity 100)` 경고가 있었다. 손실 id 는 버스트 끝쪽(184–199)이다. 원 D2 출력 `received=196 split=100/96`(같은 하네스·같은 커밋의 1차 실행은 `200/200 split=102/98`, `runs/radar-spike-sqlite-layer-20261007-161819/verify.log`)과 같은 모양이다.
- 모든 시도에서 송신 창 ±50ms 안의 취소는 **0회**였다. 버스트 동안은 메시지가 계속 와 0.5초 타임아웃이 나지 않는다.
- 왜 버스트가 한 번에 쌓이나(코드상 확인): nats-py 의 구독 태스크는 대기열이 비지 않는 동안 콜백을 연달아 부르고, 레이어 콜백(`_enqueue`)은 기다리지 않는다. 그래서 한 읽기 버퍼에 든 메시지가 수신 태스크가 돌기 전에 mailbox 에 다 들어간다. 큐 그룹 분배가 100 을 넘은 쪽이 넘친 만큼 버린다.

| D2 원인 후보 | 판정 |
|---|---|
| mailbox 용량 드롭 | **재현함**(위 표). `cap1000` 에서 사라진다 |
| 취소 공백(0.5초 폴링) | 이 꼴에서는 **재현 안 됨**(송신 창 안 취소 0회). 취소 공백 손실 자체는 P-cancel(10ms)에서 따로 재현함 |
| 예외 집계(`except Exception` 을 `ChannelFull` 로 셈) | 원인 아님 — 원 출력 `sent_ok=200 ChannelFull=0`(코드상 확인). 이 랩 실행기는 예외를 종류째 남기고 송신 예외 0 |
| 중복 집계(두 수신자 교집합만) | 200 미만을 설명하지 못함(같은 수신자 안의 중복은 `received` 를 늘린다, 코드상 확인). 이 랩은 워커 안·사이 중복 모두 0 |
| 준비 확인 없음(`sleep(1.5)`) | 주 원인 아님(추론): 한쪽이 구독 전이면 다른 쪽이 거의 다 받아 손실이 ~100 이어야 하는데 원 출력은 100/96 |

결론: channels-nats 결함이 아니라 README 가 밝힌 '받는 쪽이 넘치는 메시지를 버린다'를 하네스의 버스트가 건드린 것이다. 운영 함정 하나가 나온다: **드롭 경고는 60초에 한 번으로 묶여, 첫 줄("1 message(s) dropped so far")만 보면 실제 손실(이 실측에서 최대 16, BURST 에서 100)을 과소평가한다.** 합계는 자리가 난 뒤 다음 메시지가 들어올 때 "has room again after dropping N" 으로 나온다.

## 5. 알려진 결함 — 일반 채널을 구독 확인 왕복보다 짧게 폴링하면 연결이 읽기를 멈춘다 (재현함)

`test_P2_nats_flush_cancel_known_defect`(strict xfail). 일반 채널을 `wait_for(receive, 1ms)` 로 폴링하는 수신을 **별도 레이어 인스턴스**(자기 NATS 연결)에 두고, 그 뒤 같은 인스턴스로 그룹 수신을 띄운다.

| 단계 | 결과(대표 실행) |
|---|---|
| 1ms 폴링(취소 7,384·7,495회) | probe 168건을 10초 동안 보내도 0건 수신(구독이 한 번도 서지 않음) |
| 같은 인스턴스의 그룹 수신 | probe 185건, 0건 수신 |
| 워커 로그 | 워커마다 `nats: encountered error [InvalidStateError: invalid state]` |
| 문서 설정의 기본 레이어(다른 연결)로 그룹 수신 | 200/200 ×2 — 결함은 그 연결에 갇힌다 |

- 원인(코드상 확인): 일반 채널 첫 `receive()` 가 구독 확인 `flush` 를 기다리는 동안 1ms 타임아웃이 그것을 취소한다. nats-py 2.16.0 의 `flush()` 는 PONG future 를 `_pongs` 에 넣고 기다리므로 취소된 future 가 거기 남는다. PONG 이 오면 `_process_pong` 이 그 future 에 `set_result` 하다 `InvalidStateError` 를 내고, `_read_loop` 는 `except Exception: _logger.error(...); break` 로 **읽기 루프를 끝낸다**(재접속·close 없음).
- 관측: 서버(`connz`)에는 그 연결의 새 SUB 가 있고 `out_msgs` 가 늘지만, 워커의 nats-py 구독 태스크는 모두 빈 대기열에서 기다린다(`/lab/chan/tasks` 덤프). 쓰기는 되고 읽기는 죽은 연결이다.
- 처음에는 1ms 폴링을 기본 레이어에서 돌려 그 뒤 모든 시나리오(BURST·NOCLOSE·D2-cap100·LIVE)가 probe 0건으로 실패했다. 결함을 별도 인스턴스에 가둔 것은 그래서다.
- 10ms 폴링(P-cancel)에서는 나지 않았다(실측 RTT 약 0.25ms). 다만 부하로 확인 왕복이 타임아웃을 넘으면 같은 경로가 열린다(원리상, 미검증).
- 미검증: 회복 여부와 시간(nats-py ping 간격 기본 120초로 멈춘 연결을 알아챌 것으로 추정, 재지 않음), 실제 Channels 컨슈머에서 구독 확인 중 취소(접속 직후 끊긴 websocket 의 `new_channel`·`group_add`)로 같은 일이 나는지.
- 부수 관측(확인 필요): 10ms 폴링 수신을 `stop`(취소)한 뒤 한 워커 연결에 `channels.ch.lab.plain.pc10…` 구독이 서버에 남아 있었다(`connz`, 보조 실행). README 는 일반 채널을 취소하면 즉시 해지한다고 쓴다.

외부 변경은 하지 않았다. 이관 문서: [`handoff-marco-nats-flush-cancel.md`](handoff-marco-nats-flush-cancel.md).

## 6. 함께 드러난 것: #66 전의 P2 는 nats 없이 돌았다 (재현함)

랩은 앱을 `docker compose up --no-deps` 로 띄운다(`_lab.Stack.up_app`). 그래서 문서 compose 의 `depends_on: [nats]` 가 무시돼 **nats 컨테이너가 뜨지 않았다.** channels-nats 를 넣은 첫 실행에서 `listen` 이 `NoServersError`(이름 해석 실패 `gaierror`)로 500 이 나 드러났다. 그 전 P2 는 channels-nats 를 설치하지 않았고 doctor 는 브로커에 접속하지 않으므로 아무것도 실패하지 않았다. 2026-10-08 의 P2 결과(워커 PID 2개, doctor OK, stop 0.72초)는 브로커 없이 잰 것이다. 지금 P2 는 앱보다 먼저 `compose up -d nats` 를 한다(문서 §4 '브로커를 먼저 띄운다').

## 7. 진단 뷰 `/lab/chan/tasks` 를 남긴 이유

§5 의 원인은 이 덤프로 좁혔다(구독 태스크가 빈 대기열에서 기다림 → 메시지가 파서까지 오지 않음 → 읽기 루프). 랩 앱에만 있고 DB 를 열지 않으며 시나리오가 부르지 않는다. 결함이 고쳐져 xfail 이 XPASS 로 바뀌거나 다른 채널 결함이 나올 때 같은 진단을 다시 쓸 수 있도록 남긴다.

## 8. 미검증

- GitHub Actions amd64 러너에서의 P2(오케스트레이터가 돌린다).
- §5 결함의 회복 시간, 실제 컨슈머 경로.
- 일반 채널 취소 뒤 서버에 남은 구독(§5 끝).
- 워커 3개 이상, 메시지 크기·속도를 바꾼 경우, 브로커 재시작 중 전달.
