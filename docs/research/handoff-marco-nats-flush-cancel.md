# 마르코 이관: 취소된 구독 확인 뒤 nats-py 읽기 루프가 끝나 연결이 조용히 죽는다

- 결정: 마스터 2026-10-26(#66 재개 지시). "이 저장소에 문서로 남기고, 다른 저장소에는 아무것도 쓰지 않는다."
- 작성: django-sqlite-ops #66 구현자. 이 문서는 재현·원인·방향 후보만 담는다. **실제 수정·이슈 등록·릴리스는 마르코 관문에서 정한다.**
- 대상: channels-nats 0.10.0(PyPI), nats-py 2.16.0. channels 4.3.2, Python 3.13, uvicorn 0.54.0, nats-server 2.15.0.

## 근거 (실측, 재현함)

django-sqlite-ops 회귀 랩 P2(`single-server-multiproc` 문서 compose, uvicorn 워커 2개)에서 결정적으로 재현했다. 결과 문서: [`lab-2026-10-26-multiproc-nats.md`](lab-2026-10-26-multiproc-nats.md) §5. 랩 시나리오 `lab/test_scenarios.py::test_P2_nats_flush_cancel_known_defect` 가 strict xfail 로 이 결함을 고정한다(고쳐지면 XPASS 로 실패해 알린다).

## 재현

1. uvicorn 워커의 이벤트 루프에서, 레이어 인스턴스 하나로 일반 채널을 `asyncio.wait_for(layer.receive("lab.plain.x"), 0.001)` 반복 폴링한다.
2. 그 채널로 메시지를 보내도 아무것도 받지 못한다(probe 168건 / 10초 동안 0건).
3. 같은 인스턴스로 `new_channel()` + `group_add()` 한 채널도 `group_send` 를 받지 못한다(185건 / 10초 동안 0건).
4. 워커 로그: `nats: encountered error` 와 트레이스백 `nats/aio/client.py … _process_pong … future.set_result(True)` → `asyncio.exceptions.InvalidStateError: invalid state`(워커마다 1회).
5. nats-server `connz?subs=1`: 그 연결에 새 SUB 가 있고 `out_msgs` 가 늘어난다. 워커의 nats-py `Subscription._wait_for_msgs` 태스크는 모두 빈 대기열에서 기다린다.
6. 다른 레이어 인스턴스(다른 연결)는 정상이다(200/200). 10ms 폴링에서는 재현되지 않았다(실측 RTT 약 0.25ms, 취소 공백 손실 16/200 만 — 계약대로).

## 원인 (코드상 확인)

| 위치 | 내용 |
|---|---|
| channels-nats `layer.py` `_mailbox` → `_subscribe` → `_flush`(0.10.0) | 일반 채널의 첫 `receive()` 는 큐 그룹 구독을 만든 뒤 서버 확인(`flush`)을 기다린다. 호출자의 `wait_for` 타임아웃이 이 대기를 취소한다 |
| nats-py 2.16.0 `Client.flush()` | PONG future 를 `self._pongs` 에 넣고 PING 을 보낸 뒤 기다린다. 대기가 취소되면 future 는 취소된 채 `_pongs` 에 남는다 |
| nats-py `Client._process_pong`(client.py 1869–1878) | `_pongs.pop(0)` 한 future 에 상태 확인 없이 `set_result(True)` → 취소된 future 면 `InvalidStateError` |
| nats-py `Client._read_loop`(client.py 2395–2427) | `except Exception as ex: _logger.error("nats: encountered error", exc_info=ex); break` — 읽기 루프를 끝내고 재접속도 close 도 하지 않는다. 바로 아래 `# except asyncio.InvalidStateError: pass` 가 주석으로 남아 있다 |

channels-nats README 는 flush 대기 중 취소를 이미 다룬다("같은 일이 구독을 확인하는 `flush()`에서도 난다 … 되살리며, 만들다 만 구독은 해지한다"). 그 처리는 호출자의 취소와 구독을 되돌리지만, nats-py `_pongs` 에 남은 취소된 future 는 건드리지 않는다.

## 영향

- 그 이벤트 루프의 **연결 전체**가 읽기를 멈춘다: 그룹·프로세스 전용·일반 채널 모두. 쓰기(`send`·`group_send`·SUB)는 계속 성공하므로 보내는 쪽도 받는 쪽도 예외를 보지 못한다. 로그는 nats-py 의 `ERROR` 한 줄뿐이다.
- 계약('일반 채널을 `wait_for` 로 폴링하면 두 폴링 사이의 메시지를 놓친다')을 넘는 손실이다.
- 실제 컨슈머 경로(미검증, 원리상 같은 경로): 구독 확인을 기다리는 `new_channel()`·`group_add()`·첫 `receive()` 가 취소되면 — 예: websocket 이 접속 직후 끊겨 컨슈머가 취소될 때 — 워커 하나의 채널 레이어가 통째로 멈출 수 있다. 부하로 확인 왕복이 길어질수록 가능성이 커진다.
- 회복: 미검증. nats-py ping(기본 120초 간격, 미응답 한도)이 멈춘 연결을 알아채 재접속할 것으로 추정한다.

## 방향 후보 (마르코 판단)

1. **nats-py 상류**: `_process_pong` 에서 `if not future.done(): future.set_result(True)`. 기능 동결 정책상 상류 이슈·PR 로 가는 것이 정석이다.
2. **channels-nats 쪽 방어**(상류가 늦을 때): 구독 확인 `flush` 를 호출자 취소에서 떼어(`asyncio.shield` 등) 끝까지 기다리게 하거나, 취소된 확인 future 를 `_pongs` 에서 걷어 낸다. README 의 '취소 되살리기' 절과 같은 자리다.
3. **문서**: README '다른 점'에 "일반 채널을 구독 확인 왕복보다 짧은 `wait_for` 로 폴링하지 않는다"를 적는다(1·2 전까지의 임시 안내).
4. 재현 테스트: 실제 nats-server 로 위 '재현' 1–3 을 그대로 돌리면 된다(10ms 판은 대조군).

## 확인 필요 (부수 관측)

- 일반 채널 수신을 취소(`stop`)한 뒤에도 한 워커 연결에 `channels.ch.lab.plain.<이름>` 구독이 서버에 남아 있었다(`connz`, 10ms 폴링 뒤, 1회 관측). README 는 일반 채널을 취소하면 즉시 해지한다고 쓴다. 해지가 늦게 나간 것인지 남은 것인지, 남는다면 그 구독이 큐 그룹 몫을 가져가는지 재현하지 않았다.

## 마르코에게 맡길 판단

- 상류(nats-py) 이슈로 갈지, channels-nats 에서 막을지, 둘 다인지.
- 릴리스·CHANGELOG 반영 여부.
- 위 부수 관측을 별도로 조사할지.
