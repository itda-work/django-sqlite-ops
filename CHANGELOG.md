# Changelog

[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따르고, 버전은 [PEP 440](https://peps.python.org/pep-0440/) 으로 매긴다. 릴리스할 때 `[Unreleased]` 의 항목을 새 버전 절로 옮긴다(`scripts/release_check.py` 가 확인한다).

> ⚠️ **실험적 프로젝트다.** 1.0 전에는 API·동작·지원 범위가 예고 없이 바뀔 수 있다. 사전 릴리스(`a`·`b`·`rc`)는 `pip install --pre` 로만 설치된다. 운영 환경에서는 버전을 고정한다.

## [Unreleased]

## [0.1.0a1] - 2026-10-26

첫 실험적 사전 릴리스다([D-28](docs/DECISIONS.md)). v0.1 범위([DESIGN §3-1](docs/DESIGN.md#3-범위))를 담는다. 지원 범위: Python 3.13·3.14, Django 5.2·6.1, SQLite 3.37.2 이상, Litestream 0.5.17. boot CLI 는 POSIX(Linux·macOS)만 지원한다.

### Added

- **권장 설정** `sqlite_database()`: WAL·`busy_timeout`·`synchronous` 등 실측 근거가 있는 PRAGMA 표 하나를 정본으로, 표준 `DATABASES` 항목을 만든다. 프로필, `pragmas`·`options` 덮어쓰기, 잘못된 입력 거부 (#1)
- **boot CLI** `python -m django_sqlite_ops.boot`: Django 를 import 하지 않고 기동 전에 DB 상태를 판정해 Litestream 복원·신규 생성·기존 DB 도입을 결정한다. 판정할 수 없으면 사유 코드와 함께 거부(exit 2)하고, 파일 잠금으로 동시 실행을 막는다 (#2·#3·#4)
- **시스템 체크** `sqlite_ops.*`: DB 를 열지 않고 설정만으로 `SQLITE_OPS` 오류, `transaction_mode`·WAL 미설정, VFS 별칭의 `CONN_MAX_AGE`, ASGI 에서의 `CONN_MAX_AGE=None` 을 알린다 (#5)
- **`sqlite_doctor` 관리 명령**: 실제 연결로 PRAGMA·WAL·Litestream 복제 상태를 진단한다. 종료 코드 0/1/2 (#6)
- **`sqlite_doctor --restore-test`**: 복제본을 별도 디렉터리에 복원해 무결성 검사·TXID 비교로 복원 가능성을 검증한다. 운영 DB 와 Litestream 메타 경로는 건드리지 않는다 (#52·#54)
- **복제 헬스 뷰**: 로컬과 복제본의 TXID 를 직접 비교해 `caught_up`·`backlog`·`unknown`(사유 코드 `stale`·`remote_error` 등)을 JSON 으로 낸다 (#7·#65·#72)
- **배포 프로필 문서**: 단일 서버, 단일 서버 다중 프로세스, fd 한도 (#9·#32)
- **compat 패치**: channels-lite aio 레이어 결함의 버전 게이트 몽키패치 (#8)
- **회귀 랩**: Docker(SeaweedFS·toxiproxy)로 실패 경계 L1–L8 을 재현하는 수동 실행 랩(`scripts/lab.sh`, GitHub Actions `lab.yml`). 기본 CI 밖이며 wheel 에는 들어가지 않는다(sdist 에는 소스로 들어간다) (#10)

### Known limitations

- 미검증 항목은 [DESIGN §12](docs/DESIGN.md#12-미검증확인-필요) 에 있다. 실제 클라우드 S3·R2·Tigris, Windows 의 boot CLI, ASGI 에서의 VFS 별칭 동작은 검증하지 않았다.

[Unreleased]: https://github.com/itda-work/django-sqlite-ops/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/itda-work/django-sqlite-ops/releases/tag/v0.1.0a1
