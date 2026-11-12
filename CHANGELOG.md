# Changelog

[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따르고, 버전은 [PEP 440](https://peps.python.org/pep-0440/) 으로 매긴다. 릴리스할 때 `[Unreleased]` 의 항목을 새 버전 절로 옮긴다(`scripts/release_check.py` 가 확인한다).

> ⚠️ **실험적 프로젝트다.** 1.0 전에는 API·동작·지원 범위가 예고 없이 바뀔 수 있다. 사전 릴리스(`a`·`b`·`rc`)는 버전을 명시해 설치한다(`pip install "django-sqlite-ops==0.1.0a2"`). `--pre` 는 모든 의존성의 사전 릴리스까지 허용하므로 쓰지 않는다. 운영 환경에서는 버전을 고정한다.

## [Unreleased]

### Added

- **Python 3.15 를 지원한다.** Django 6.1(3.15 를 지원하는 6.1.3 이상)과 함께다. Django 5.2 는 Python 3.14 까지 지원하므로 3.15 × Django 5.2 는 지원 범위가 아니다. CI 가 3.15 에서 전체 테스트와 실제 Litestream(0.5.17·0.5.18)·channels-lite 통합 테스트를 돌고, classifier 에 `Programming Language :: Python :: 3.15` 를 넣었다 (#101)
- **시스템 체크 `sqlite_ops.W006`: `ATOMIC_REQUESTS` 와 `transaction_mode` `IMMEDIATE`(또는 `EXCLUSIVE`)를 함께 쓰면 알린다.** 이 조합에서는 뷰마다 `BEGIN IMMEDIATE` 가 쓰기 잠금을 뷰 끝까지 쥐어 읽기만 하는 뷰도 직렬로 돈다(0.3초 뷰 4개 동시 호출에 0.32·0.67·0.99·1.31초, 끄면 모두 0.32초 — 재현함). 항상 도는 체크이고 DB 를 열지 않는다. 읽기 전용·VFS·메모리 DB 별칭에는 내지 않는다. 힌트는 `ATOMIC_REQUESTS` 를 끄고 쓰는 뷰에만 `transaction.atomic()` 을 쓰거나 읽기 뷰에 `transaction.non_atomic_requests` 를 붙이라고 안내한다. 설정은 바꾸지 않는다. 의도한 구성이면 `SILENCED_SYSTEM_CHECKS` 로 끈다. `check --fail-level WARNING` 을 쓰는 CI 는 이 조합에서 실패로 바뀐다 (#102)
- **`sqlite_doctor` 가 SQLite WAL-reset 버그의 영향 범위인 판을 알린다.** SQLite 공식 문서(https://sqlite.org/wal.html#the_wal_reset_bug, 2026-11-12 확인)의 범위(3.7.0~3.51.2, 3.51.3 에서 수정, 3.44.6·3.50.7 로 백포트)에 들면 `sqlite_version` 줄에 메시지를 적는다. 판 번호만 보므로 배포판이 판 번호를 바꾸지 않고 패치했을 수 있다. **수준은 `ok` 이고 종료 코드는 바뀌지 않는다**(프로필 이미지 `python:3.13-slim` 의 3.46.1 도 범위 안이다). `--json` 의 키는 그대로다. 판정 함수 `doctor.wal_reset_affected()` 를 둔다 (#103)

### Changed

- **Litestream 0.5.18 을 검증한 판으로 받는다.** boot·`sqlite_doctor` 의 Litestream 설정 대조·헬스·`--restore-test` 가 0.5.17 과 0.5.18 을 받고 그 밖의 판은 계속 거부한다. 0.5.18 실측 fixture(`file://` 복제본의 `ltx`·`databases`·`restore`)의 출력 형식·rc·오류 문구는 0.5.17 과 같았다(시각·크기·압축 묶음만 다름). 배포 프로필 `single-server` 의 Dockerfile 이 0.5.18 을 설치한다 (#99)
- **S3 가 연결을 거부할 때 0.5.18 은 더 빨리 실패한다.** 0.5.17 의 `ltx` 는 30초 타임아웃까지 매달려 사유가 `timed out` 이었고, 0.5.18 은 약 8초 뒤 `exited with 1: … exceeded maximum number of attempts` 로 끝난다. boot 의 거부(exit 2, `remote_error`)와 헬스의 `unknown` 은 두 판에서 같고, 헬스의 `stale`↔`remote_error` 주기만 짧아진다. 0.5.18 은 이때 Litestream ERROR 로그도 남긴다 (#99)

### Fixed

- **Litestream 버전 게이트에서 거부할 때 boot 의 힌트가 그 원인을 말한다.** 지금까지는 검증하지 않은 판·바이너리 없음·버전 출력을 읽을 수 없음도 복제본 접근(endpoint·자격 증명·네트워크)을 고치라고 안내했다. 이제 설치된 판과 검증한 판 목록, 바이너리 경로와 설치 안내, 받은 출력을 보여 주고 검증한 판을 설치하거나 `--litestream` 으로 가리키라고 한다. 사유 코드(`remote_error`)·종료 코드(2)·판정은 그대로다. `check_version()` 은 사유 문자열 대신 `VersionProblem`(종류·사유·세부)을 돌려주고, `RemoteError` 에 실패 종류(`failure`)와 세부(`detail`)가 붙는다 (#100)
- **원격 조회가 실패한 채 `stale_meta`·`empty_db`·`orphan_sidecars` 로 거부될 때 힌트가 조회부터 고치라고 한다.** 지금까지는 조회가 실패한 동안에도 `--on-unknown restore` 를 권해 그대로 다시 돌리면 같은 거부로 돌아왔다. 이제 조회 실패의 원인과 대처가 먼저, `then:` 뒤에 조회가 된 뒤 할 일이 나온다. `empty_db` 힌트 끝의 "if the remote lookup failed, fix replica access first" 는 이 앞부분으로 옮겨 지웠다 (#100)

## [0.1.0a2] - 2026-10-27

의존성 하한과 문서를 고친 사전 릴리스다. 패키지 코드(`django_sqlite_ops/`)는 0.1.0a1 과 같다. 저장소 안의 회귀 랩·CI 정비(#88·#90·#94)는 sdist 에는 실리지만 설치되는 코드(wheel)에는 영향이 없으므로 여기 적지 않는다.

### Changed

- **`[nats]` extra 가 `channels-nats>=0.10.1` 을 요구한다.** 0.10.0 이하에서는 구독 확인이 취소되거나 시간 초과되면 그 워커의 채널 수신이 예외 없이 멈춘다. 0.10.1 은 같은 API 의 패치 릴리스다 (#91·#88)
- **README(PyPI 설명)를 고쳤다.** '왜 쓰는가' 절에 Django 기본 설정 대 권장 설정, `CONN_MAX_AGE` 의 실측 차트와 boot 의 실패 경계 L1–L8 표를 넣었다. 나빠진 지표와 재현되지 않은 이득도 함께 적는다 (#83). 설치 절에 channels-nats 하한 안내를 넣었다 (#91). 문제 해결 '채널 레이어'의 channels-nats 결함 안내를 0.10.1 기준으로 고쳤다 (#88)
- 배포 프로필 `single-server` 의 `requirements.txt` 예가 이 패키지를 PyPI 에서 받는다 (#84)

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

[Unreleased]: https://github.com/itda-work/django-sqlite-ops/compare/v0.1.0a2...HEAD
[0.1.0a2]: https://github.com/itda-work/django-sqlite-ops/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/itda-work/django-sqlite-ops/releases/tag/v0.1.0a1
