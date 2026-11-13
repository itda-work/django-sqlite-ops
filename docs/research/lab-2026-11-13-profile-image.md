# 프로필 이미지의 SQLite 와 WAL-reset 버그 (#117, 2026-11-13)

배포 프로필 `single-server` 의 Dockerfile 을 `python:3.13-slim`(SQLite 3.46.1)에서 `debian:trixie-slim` + uv 가 받는 Python 3.14(python-build-standalone, SQLite 3.53.1)로 옮기며 확인한 것이다. 근거 등급은 **재현함**(직접 실행) / **문서 확인**(1차 출처를 읽음) / **미검증** 으로 적는다.

호스트: macOS, colima 0.10.3(linux/arm64, amd64 는 QEMU 에뮬레이션), Docker 29.5.2, Compose 5.5.1, uv 0.13.7(호스트).

## 1. 배포판과 이미지의 SQLite (2026-11-13)

배포판 상태는 컨테이너 안에서 `apt-cache policy libsqlite3-0` 와 `apt-get changelog`(Debian 은 metadata.ftp-master.debian.org, Ubuntu 는 changelogs.ubuntu.com)로 읽었다. Ubuntu 는 `-proposed` 를 켜고 후보만 봤고 설치하지는 않았다. 모두 **재현함**(arm64, 표의 이미지 행은 amd64 도).

| 대상 | 판 | WAL-reset 수정 |
|---|---|---|
| Debian trixie | `3.46.1-7+deb13u2`(2026-06-14, FTS5 CVE-2026-11822·11824) | 없음 |
| Debian bookworm | `3.40.1-2+deb12u2`(2025-08-26) | 없음 |
| Debian testing·unstable(forky) | `3.54.1-2` | 상류 판이 고쳐짐(안정판 아님) |
| Ubuntu 24.04 | `-updates` `3.45.1-1ubuntu2.9`, `-proposed` `3.45.1-1ubuntu2.10` | `-proposed` 에만 "Fix WAL-Reset corruption race. (LP: #2171337)"(2026-10-21) |
| Ubuntu 22.04 | `-updates`·`-security` `3.37.2-2ubuntu0.8`, `-proposed` `3.37.2-2ubuntu0.9` | 같음(`-proposed` 에만) |
| `python:3.13-slim`·`3.14-slim`·`3.15-slim` | Python 3.13.18·3.14.10·3.15.2 → `/lib/<arch>/libsqlite3.so.0`(`ldd`) = 3.46.1 | 없음 |
| `python:3.13-slim-bookworm`·`3.14-slim-bookworm` | 3.40.1 | 없음 |
| `ghcr.io/astral-sh/uv:0.13.7-python3.14-trixie-slim` | Python 은 `python:3.14-slim` 그대로, 3.46.1 | 없음 |
| uv 가 받는 Python(uv 0.13.7·0.13.8) | 3.13.18·3.14.10·3.15.2, `_sqlite3` 가 builtin(정적) = **3.53.1** | 고쳐짐 |
| `pysqlite3-binary` 0.5.4.post2 | amd64 cp314 wheel 만, 3.51.1 | 없음(영향 범위). arm64 wheel 없음 |
| Litestream 0.5.17·0.5.18 `.deb` | 정적(NEEDED 0), `modernc.org/sqlite@v1.49.1`, SQLITE_SOURCE_ID `2026-04-09 11:41:38 4525003a…` = **3.53.0** | 고쳐짐 |

- 날짜와 판의 대응은 https://sqlite.org/chronology.html 로 확인했다(문서 확인).
- PBS 의 SQLite 판은 `pythonbuild/downloads.json` 의 `3530100` 이다(문서 확인, main).
- Ubuntu 의 패치는 패키지 판(`ubuntu2.10`)이 붙는다. 그러나 `sqlite3.sqlite_version` 은 상류 판(3.45.1·3.37.2) 그대로다. 그래서 doctor 는 이 패치 판에도 메시지를 낸다(#103 의 결정).

## 2. 철회판 3.52.0 은 수정을 포함한다

sqlite.org 의 1차 출처(문서 확인, 2026-11-13):

- wal.html §11: "On 2026-03-03, one of the SQLite developers (Dan) found and fixed a bug." 수정은 3.51.3(2026-03-13)부터이고, 3.44.6·3.50.7 로 백포트됐다.
- 수정 check-in [`7168988acb`](https://sqlite.org/src/info/7168988acb)(2026-03-03 19:43, trunk, "Avoid an obscure race condition between a checkpointer and a writer wrapping around to the start of the wal file.", `src/wal.c` 와 `test/walrestart.test`).
- `version-3.52.0` = [`557aeb4386`](https://sqlite.org/src/info/version-3.52.0)(2026-03-06 16:01, trunk).
- 타임라인 `timeline?p=version-3.52.0&bt=7168988acb` 은 "8 ancestors of version-3.52.0 back to 7168988acb" 이다. 수정이 3.52.0 의 조상이라는 뜻이다.
- 3.52.0 의 releaselog 는 "Withdrawn" 이고 기능을 3.53.0 으로 옮겼다고만 적는다. WAL-reset 은 언급하지 않는다.

그래서 `doctor.wal_reset_affected("3.52.0")` 이 `False` 인 것은 맞다. 판정 코드는 바꾸지 않았다. 테스트 표에 3.52.0·3.53.0 을 더해 고정했다. #117 기획 때의 추정('수정 전에 나왔으니 영향 범위일 가능성')은 틀렸다.

## 3. 이미지

| | 기준 | Python | SQLite | 크기(`docker image ls`) |
|---|---|---|---|---|
| 이전 프로필(문서 블록, 시제품) | `python:3.13-slim` `sha256:e48f59cd…` | 3.13.18 `/usr/local/bin/python` | 3.46.1(Debian 동적) | arm64 376MB, amd64 355MB |
| 새 프로필(문서 블록, 시제품) | `debian:trixie-slim` `sha256:a72b4433…` + `ghcr.io/astral-sh/uv:0.13.8` `sha256:5cc1805e…` | 3.14.10 `/opt/venv/bin/python` | 3.53.1(정적) | arm64 475MB, amd64 492MB |
| 랩 앱 이미지 `dso-lab-app:<sha>-ls0.5.18` | 위 문서 블록 + 작업 트리 wheel | 이전 / 새 | 3.46.1 / 3.53.1 | arm64 377MB → 477MB |

- 시제품은 문서 블록에 빈 `litestream.yml`·`entrypoint.sh` 와 문서의 `requirements.txt` 를 넣어, 저장소 밖 임시 디렉터리에서 빌드했다.
- 새 이미지에서 확인한 것(arm64·amd64 재현함): Django 6.1.3·uvicorn 0.54.0 설치, 앱 사용자 uid 10001 로 `python -m django_sqlite_ops.boot --help` 실행, `litestream version` 0.5.18, `pip` 없음(`uv pip` 사용).
- Python 빌드의 OpenSSL 은 3.5.9 다(이전 이미지의 Debian OpenSSL 은 3.5.7).
- `debian:trixie-slim` 에는 Debian `libsqlite3-0` 3.46.1 이 남아 있지만, Python·Litestream 은 그것을 쓰지 않는다.
- ctypes 로는 새 SQLite 에 닿지 않는다(재현함). `ctypes.util.find_library("sqlite3")` 는 `libsqlite3.so.0`(Debian 3.46.1)을 찾고, `CDLL(None)` 은 `undefined symbol: sqlite3_libversion` 이다. 그래서 `lab.sh cache` 의 `_sqlite_lib()` 는 `None` 이 되고 메모리 통계가 빈다.
- `UV_MANAGED_PYTHON=1`(uv 환경 변수 문서)로 빌드해 위 판을 얻었다. 기획 때의 시제품은 `UV_PYTHON_PREFERENCE=only-managed` 였다. 기준 이미지에 시스템 Python 이 없어서 두 설정의 차이는 이 실험에서 드러나지 않는다.

## 4. 회귀 랩 (arm64)

`scripts/lab.sh run all`, 브랜치 `issue-117-profile-sqlite`(작업 트리, 커밋 전), Litestream 0.5.18: **15 passed**(18분 12초). L1–L9(L8a–d·L9a·L9b 포함), P1, P2 모두 통과했다. 결과 `lab/.out/results-20261113-175845.jsonl`(git 무시).

- P1(46.1초): 빈 볼륨 소유자 10001, `check --deploy` 의 `sqlite_ops.*` 없음, doctor exit 0, `docker stop` exit 0·0.5초, 복제본 400행, 재부팅 `match`.
- P2(238.6초): 채널 시나리오 문제 0, channels-nats 0.10.1·Django 6.1.3·uvicorn 0.54.0. P2 이미지는 `nats.Dockerfile` 의 `uv pip install` 로 빌드됐다.
- L6: `PYTHONPATH=/app/lab_hooks` 의 `sitecustomize` 훅이 venv Python 에서도 걸렸다. 결과의 `renames` 에 `[lab-hook] rename 1..` 줄이 있다(재현함).
- 랩 결과에는 doctor 의 `sqlite_version` 줄이 남지 않는다. 그 줄은 5절에서 확인했다.
- amd64(GitHub Actions `lab.yml`)는 PR 을 연 뒤 돌린다. 결과는 PR·이슈에 적는다.

#117 전후의 랩 측정은 Python·SQLite 판이 달라 직접 비교하지 않는다(`lab/README.md`).

## 5. 기존 볼륨 이어받기 (한 번, 랩 밖)

작업 디렉터리의 일회용 스크립트(저장소에는 넣지 않는다. 절차는 이 절과 이슈 #117 코멘트에 남긴다)로 돌렸다. 랩 이미지 두 개를 쓰고, 복제본은 같은 볼륨 안의 `file:///data/replica` 이며, S3 는 쓰지 않는다. 재현함(arm64).

1. 옛 이미지(`python:3.13-slim`, SQLite 3.46.1)로 `--init-new` 첫 부팅: `decision: state=fresh action=proceed reason_code=new_db`. 300행을 쓰고 `docker stop`, exit 0.
2. 같은 볼륨으로 새 이미지(SQLite 3.53.1)를 플래그 없이 재부팅: `decision: state=match action=proceed reason_code=local_current reason=local txid 3 >= remote txid 3`. 300행이 그대로 있고, 50행을 더 써 350행이 됐다. `sqlite_doctor` 는 exit 0 이고 `OK default sqlite_version 3.53.1` 에 **WAL-reset 메시지가 없다.** `journal_mode` 는 WAL 이다.
3. 정지 exit 0. 새 이미지로 볼륨을 읽으면 `app.sqlite3` 350행, 소유자 10001, 로컬 L0 와 복제본 L0 의 해시가 같다.

## 6. 빌드 가드 변이

일회용 스크립트(저장소 밖 임시 디렉터리에서 빌드)로 확인했다. 문서 블록에서 세 줄만 바꿨다: `FROM python:3.13-slim`, `UV_PYTHON_DOWNLOADS=never`, `uv venv /opt/venv --python /usr/local/bin/python3`. 빌드 로그에 `sqlite 3.46.1` 이 찍히고 가드 줄이 exit code 1 로 빌드를 멈췄다(재현함, arm64). 바꾸지 않은 블록은 랩(4절)이 빌드했다.

## 미검증

- 연결 하나만 고친 SQLite 를 쓸 때(예: 공식 `python:*-slim` 의 Python 3.46.1 + Litestream 3.53.0)의 안전성. SQLite 문서에 없다.
- Ubuntu `-proposed` 패치가 상류 수정과 같은지.
- amd64 랩(PR 뒤에 돌린다).
- 이 패키지 구성에서 버그가 실제로 일어나는지. 재현을 시도하지 않았다.
