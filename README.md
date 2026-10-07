# django-sqlite-ops (가칭)

Django 에서 SQLite 를 운영 DB 로 안전하게 쓰기 위한 **운영 도구**다. 아직 구현 전이며, PyPI 배포 전이다.

- 권장 설정: `sqlite_database(path, profile="single-server", pragmas=None, options=None)` 가 실측 근거가 있는 값(`transaction_mode=IMMEDIATE`, `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`)을 담은 표준 `DATABASES` 항목을 돌려준다. `pragmas={...}` 로 PRAGMA 를 덮거나 더하고(`None` 이면 뺀다), `options={...}` 로 `OPTIONS` 키를 더한다. PRAGMA 이름은 대소문자를 가리지 않는다.
  대기 시간은 `busy_timeout`(밀리초, 기본 5000)이 `options` 의 `timeout`(초)보다 우선한다. `timeout` 을 쓰려면 `pragmas={"busy_timeout": None}` 을 함께 주고, 아니면 `pragmas={"busy_timeout": 20000}` 처럼 덮는다.

  ```python
  from django_sqlite_ops.database import sqlite_database

  DATABASES = {"default": sqlite_database(BASE_DIR / "app.sqlite3")}
  ```
- 부팅 판정·복원 CLI: Litestream 복제본과 로컬 DB 를 비교한다. 판정할 수 없으면 기동을 거부한다.
- `sqlite_doctor`: 실제 PRAGMA, 마운트, Litestream 설정 대조, 채널 레이어 의미론을 진단한다.
- 복제 헬스: `caught_up / backlog / unknown`
- 배포 프로필: `single-server`(InMemory), `single-server-multiproc`(channels-nats 추천, channels_redis 호환)

설계서: [`docs/DESIGN.md`](docs/DESIGN.md) · 결정: [`docs/DECISIONS.md`](docs/DECISIONS.md) · 근거: [`docs/research/`](docs/research/)

## 개발

```bash
uv venv && uv pip install -e . --group dev
uv run --no-sync pytest

scripts/ci-local.sh                 # GitHub Actions CI 를 act 로 로컬 실행 (lint + 전체 매트릭스)
scripts/ci-local.sh test 3.14 6.1   # 매트릭스 한 칸만
```
