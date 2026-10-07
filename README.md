# django-sqlite-ops (가칭)

Django 에서 SQLite 를 운영 DB 로 안전하게 쓰기 위한 **운영 도구**다. 아직 구현 전이며, PyPI 배포 전이다.

- 권장 설정: `sqlite_database()` 가 실측 근거가 있는 PRAGMA·`transaction_mode` 를 담은 표준 `DATABASES` 항목을 돌려준다.
- 부팅 판정·복원 CLI: Litestream 복제본과 로컬 DB 를 비교한다. 판정할 수 없으면 기동을 거부한다.
- `sqlite_doctor`: 실제 PRAGMA, 마운트, Litestream 설정 대조, 채널 레이어 의미론을 진단한다.
- 복제 헬스: `caught_up / backlog / unknown`
- 배포 프로필: `single-server`(InMemory), `single-server-multiproc`(channels-nats 추천, channels_redis 호환)

설계서: [`docs/DESIGN.md`](docs/DESIGN.md) · 결정: [`docs/DECISIONS.md`](docs/DECISIONS.md) · 근거: [`docs/research/`](docs/research/)
