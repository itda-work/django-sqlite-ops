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
6. 다른 레이어 인스턴스(다른 연결)는 정상이다(200/200). 10ms 폴링은 대개 계약대로(취소 공백 손실 16/200 만)였지만, **2026-10-26 P2 6회 중 1회는 10ms 에서도 같은 결함이 났다**(워커 하나에 `InvalidStateError`, 같은 인스턴스의 그룹 수신 probe 0/508). 실측 RTT 는 약 0.25ms 였으니, 확인 왕복이 가끔 10ms 를 넘는다는 뜻이다. 안전한 폴링 간격은 없다.

## 원인 (코드상 확인)

확인 방법: nats-py 2.16.0 소스는 `pip download nats-py==2.16.0 --no-deps --no-binary :all:`(sdist, `src/nats/aio/client.py`)로 받아 본다. 이 저장소의 venv 에는 nats-py 가 없다(`[nats]` extra 는 dev 그룹에 들지 않는다). 아래 줄 번호는 그 sdist 기준이다.

원인은 확인 대기에 남는 **취소된 PONG future** 다. 그것이 생기는 경로는 셋이다.

| 위치 | 내용 |
|---|---|
| channels-nats `layer.py` `_flush`·`_flush_or_undo`(0.10.0) | 구독 확인은 `new_channel()`·`group_add()`·일반 채널의 첫 `receive()` 가 기다린다 |
| nats-py 2.16.0 `Client.flush()`(client.py 1251–1280) | `_send_ping` 이 PONG future 를 `self._pongs` 에 넣고(2343) PING 을 보낸 뒤 `asyncio.wait_for(future, timeout)` 로 기다린다 |
| 경로 1 — 호출자 취소(**재현함**) | 호출자의 `wait_for` 타임아웃 등으로 대기가 취소되면 future 는 취소된 채 `_pongs` 에 남는다. 1ms 폴링이 이 경로다 |
| 경로 2 — nats-py 자신의 flush 타임아웃(코드상 확인) | `except asyncio.TimeoutError: future.cancel(); raise FlushTimeoutError`(1278–1280). 기본 `timeout` 은 `DEFAULT_FLUSH_TIMEOUT = 10`초(97)다. 브로커가 10초 넘게 PONG 을 늦추면 취소된 future 가 남는다 |
| 경로 3 — 레이어의 `publish_deadline`(코드상 확인) | channels-nats `_within_deadline` 은 `asyncio.timeout(publish_deadline)`(기본 10초) 안에서 `client.flush()` 를 기다린다. 기한이 지나면 그 대기를 취소하고 `FlushTimeoutError` 로 올린다. 안쪽 future 는 경로 1 과 같이 남는다 |
| nats-py `Client._process_pong`(client.py 1869–1878) | `_pongs.pop(0)` 한 future 에 상태 확인 없이 `set_result(True)` → 취소된 future 면 `InvalidStateError` |
| nats-py `Client._read_loop`(client.py 2395–2427) | `except Exception as ex: _logger.error("nats: encountered error", exc_info=ex); break` — 읽기 루프를 끝내고 재접속도 close 도 하지 않는다. 바로 아래 `# except asyncio.InvalidStateError: pass` 가 주석으로 남아 있다 |

세 경로 모두, PONG 이 **나중에라도** 오면 `_process_pong` 에서 같은 일이 난다. 경로 2·3 은 브로커가 잠시 멈췄다가 살아나는 경우다(재현하지 않음).

channels-nats README 는 flush 대기 중 취소를 이미 다룬다("같은 일이 구독을 확인하는 `flush()`에서도 난다 … 되살리며, 만들다 만 구독은 해지한다"). 그 처리는 호출자의 취소와 구독을 되돌리지만, nats-py `_pongs` 에 남은 취소된 future 는 건드리지 않는다.

## 영향

- 그 이벤트 루프의 **연결 전체**가 읽기를 멈춘다: 그룹·프로세스 전용·일반 채널 모두. 쓰기(`send`·`group_send`·SUB)는 계속 성공하므로 보내는 쪽도 받는 쪽도 예외를 보지 못한다. 로그는 nats-py 의 `ERROR` 한 줄뿐이다.
- 그 뒤의 구독 확인도 **확인 없이 성공**한다(코드상 확인). nats-py `flush()` 는 `_reading_task.done()` 이면 PING/PONG 대신 `_flush_pending()` 만 하고 돌아간다(client.py 1265–1272). 그래서 이후 `new_channel()`·`group_add()` 가 정상 반환하고, 호출자는 받지 못할 채널을 손에 쥔다. "아무 예외도 안 보인다" 의 직접 원인이다. 랩의 `after-1ms` 가 매달리지 않고 준비 기한까지 probe 만 쌓인 것과 맞는다.
- 계약('일반 채널을 `wait_for` 로 폴링하면 두 폴링 사이의 메시지를 놓친다')을 넘는 손실이다.
- 실제 컨슈머 경로(미검증, 원리상 같은 경로): 구독 확인을 기다리는 `new_channel()`·`group_add()`·첫 `receive()` 가 취소되면 워커 하나의 채널 레이어가 통째로 멈출 수 있다. 예를 들어 websocket 이 접속 직후 끊겨 컨슈머가 취소될 때다. 부하로 확인 왕복이 길어질수록 가능성이 커진다. 브로커가 10초 넘게 멈췄다 살아나는 경우(경로 2·3)에는 그동안 확인을 기다리던 모든 연결이 대상이다.
- 회복: 미검증. nats-py ping(기본 120초 간격, 미응답 한도)이 멈춘 연결을 알아채 재접속할 것으로 추정한다.

## 방향 후보 (마르코 판단)

1. **nats-py 상류**: `_process_pong` 에서 `if not future.done(): future.set_result(True)`. 기능 동결 정책상 상류 이슈·PR 로 가는 것이 정석이다.
2. **channels-nats 쪽 방어**(상류가 늦을 때): 두 방법이 있고, 덮는 범위가 다르다.
   - **취소된 확인 future 를 `_pongs` 에서 걷어 낸다.** 확인 뒤 읽기 루프가 살아 있는지 보고 연결을 다시 여는 방법도 같은 범위다. 세 경로를 모두 덮는다. 다만 nats-py 내부(`_pongs`·`_reading_task`)에 기대므로 판 고정과 호환 테스트가 따라야 한다.
   - **구독 확인 `flush` 를 `asyncio.shield` 로 감싼다.** 이것은 **경로 1 만 막는다.**
     - 경로 2 는 shield 안쪽에서 nats-py 가 스스로 `future.cancel()` 하므로 그대로 남는다.
     - 경로 3 은 shield 를 deadline 안쪽에 두면 바깥 대기만 끊긴다. 안쪽 flush 는 계속 돌다 경로 2 로 이어진다. 바깥에 두면 매달린 확인이 상한을 잃는다(#33·#34 가 막은 것).
   README 의 '취소 되살리기' 절과 같은 자리다. 모든 경로를 덮는 것은 1(상류)과 위의 첫 방법뿐이다.
3. **문서**: README '다른 점'에 임시 안내를 적는다(1·2 전까지): "구독 확인(`new_channel`·`group_add`·일반 채널 첫 `receive`)이 취소되거나 시간 초과되면 그 연결이 읽기를 멈춘다. 일반 채널을 짧은 `wait_for` 로 폴링하는 것이 그 한 예다."
4. 재현 테스트: 실제 nats-server 로 위 '재현' 1–3 을 그대로 돌리면 된다(10ms 판은 대조군).

## 확인 필요 (부수 관측)

- 일반 채널 수신을 취소(`stop`)한 뒤에도 한 워커 연결에 `channels.ch.lab.plain.<이름>` 구독이 서버에 남아 있었다(`connz`, 10ms 폴링 뒤, 1회 관측). README 는 일반 채널을 취소하면 즉시 해지한다고 쓴다. 해지가 늦게 나간 것인지 남은 것인지, 남는다면 그 구독이 큐 그룹 몫을 가져가는지 재현하지 않았다.

## 마르코에게 맡길 판단

- 상류(nats-py) 이슈로 갈지, channels-nats 에서 막을지, 둘 다인지.
- 릴리스·CHANGELOG 반영 여부.
- 위 부수 관측을 별도로 조사할지.
