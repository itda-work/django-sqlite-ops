# 마르코 이관: 채널 레이어 추천 문구 조정 (channels-nats 추천 / channels_redis 호환)

- 결정: 마스터 2026-10-07, Discord #수련장-django 스레드 "SQLite Django 패키지 설계 검토"
- 결정 원문: "문구를 조정하고 `channels-nats` 를 추천으로 넣고, `channels_redis` 는 호환 레이어 정도로만 소개토록 하자."
- 판정·근거 작성: 아이스버그(itda-django). 이 문서는 감수 판정과 수정 방향만 담는다. 실제 수정·이슈 등록·릴리스는 마르코 관문에서 정한다.

## 근거 (실측)
- 프로세스 간 레이어 벤치(Django 6.1.2, Channels 4.3.2, Python 3.13.15, macOS 로컬, 부하 수치 아님)
  - 로그: `~/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/verify2.log`
  - channels-nats 1:1 p50 0.3ms, 200멤버 fan-out 1ms, 중복 0
- wireview 기존 실측: Redis 대비 이벤트 처리량 동등, 브로드캐스트 37% 빠름, 연결당 메모리 8KB 적음
  - 근거: `django-wireview/docs/design/transport-abstraction.md:142-163`
- 방향: 신규 SQLite 운영 패키지(가칭 django-sqlite-ops)의 표준 조합이 "SQLite + Litestream + channels-nats"입니다. 초안: `~/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md`

## 바꿀 자리 (file:line, 현재 문구 요지 → 방향)

### channels-nats
1. `README.md:26` "새 프로젝트이고 그런 이유가 없다면 `channels_redis`를 먼저 검토한다"
   - 방향: 삭제합니다. 대신 "itda 스택(wireview, SQLite 단일 서버)의 추천 레이어"로 바꿉니다.
   - **기능 동결(새 기능 없음)은 그대로 둡니다.** 위상 절의 동결과 "추천"은 양립합니다. 동결은 기능 범위의 이야기이고, 추천은 사용처의 이야기입니다.
   - 재판단 조건(`README.md` 위상 절)에 "SQLite 단일 서버 표준 조합으로 채택(2026-10-07)"을 기록할지는 마르코가 정합니다.
2. `README.md:42` 플랫폼 표(NATS vs Valkey/Redis): 유지합니다. 호환 대안으로 비교하는 용도로 맞습니다.

### django-wireview
3. `README.md:183-184` 레이어 목록: channels-nats는 "추천"으로 둡니다. channels_redis는 "호환: Redis가 이미 있는 인프라. 릴리스 E2E로 호환을 확인한다(README:105)"로 낮춥니다.
4. `README.md:107`, `README.md:200` "NATS나 Redis", "channels-nats나 channels_redis": 순서는 이미 NATS가 먼저입니다. 표현을 "channels-nats(추천), 또는 호환 레이어 channels_redis"로 맞춥니다.
5. `docs/DEPLOYMENT.md:65-67` "channels_redis도 완전히 지원하며, Redis가 이미 인프라에 있다면 그쪽이 맞다": "호환 레이어로 지원(E2E 확인)"으로 바꿉니다. `### 운영: Redis`(:87~)는 절 제목을 "호환: Redis"로 바꾸고 내용은 유지합니다. 샤딩 레시피는 정확한 정보라 지우지 않습니다.
6. `docs/design/transport-abstraction.md:163` 결론 "Redis가 이미 있으면 channels_redis를 쓴다": 설계 기록(날짜가 박힌 결정 문서)이므로 **본문은 고치지 않습니다.** 2026-10-07 결정을 덧붙이는 절(5-x)을 추가하는 쪽을 권합니다.
7. 코드 문자열(사용자에게 보이는 메시지)
   - `wireview/checks.py:517` "channels_redis or channels-nats" → "channels-nats (recommended) or channels_redis"
   - `wireview/core/transport.py:74-76` NO_CHANNEL_LAYER 메시지도 같게 맞춥니다.
   - 두 문자열을 고정하는 테스트는 없습니다(2026-10-07 `grep -rn` 결과 tests/ 0건). wireview 문서 가드 테스트가 README 문구를 보는지는 마르코 쪽에서 확인합니다.

## 위험: 추천을 바꿔도 지워서는 안 되는 것
- **의미론 차이는 그대로 남겨야 합니다.** channels-nats README "Channels 규약과 다른 점"(:70-95) 절과 wireview `transport-abstraction.md:167~`(레이어별 의미론이 wireview에서 어떻게 보이는지)은 추천을 바꿔도 유지합니다.
  - 순서 보장, 수신자가 생기기 전 메시지 보관, `ChannelFull` 미발생: 이 셋은 channels_redis가 실제로 더 낫습니다.
  - 오늘 실측 D1도 같습니다. 수신자 생성 전에 보낸 메시지가 NATS에서는 0/5, SQLite 레이어에서는 5/5였습니다.
  - README:26의 "순서 보장·단절 중 보관·운영 도구 면에서 그쪽이 낫다"는 문장은 지우지 말고, "이것이 필요하면 호환 레이어(channels_redis)"라는 **선택 기준**으로 옮깁니다. 그래야 추천이 과장되지 않습니다.
- "배포 사례가 우리뿐"(`transport-abstraction.md:163`)이라는 성숙도 사실도 유지하는 편이 정직합니다.

## 마르코에게 맡길 판단
- 이슈를 저장소별로 나눌지(channels-nats 문서 1건, wireview 문서+문자열 1건)
- 릴리스 노트에 넣을지(사용자에게 보이는 체크 메시지가 바뀜)
- 위 6번(설계 기록)의 처리 방식
