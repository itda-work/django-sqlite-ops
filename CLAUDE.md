# django-sqlite-ops AI Guide

> Django 에서 SQLite 를 운영 DB 로 안전하게 쓰게 하는 운영 도구다. 권장 설정, 부팅 판정·복원, 진단, 복제 헬스, 배포 프로필을 맡는다. 채널 레이어와 DB 백엔드 래핑은 만들지 않는다.

## 정본
| 무엇 | 정본 |
|---|---|
| 설계·범위·인터페이스 | `docs/DESIGN.md` |
| 결정과 그 근거 | `docs/DECISIONS.md` |
| 실측 근거(수치) | `docs/research/*.md` |
| 스파이크 코드(그대로 쓰지 말 것) | `docs/reference/` |

## 상태
- 구현 전이다. `docs/DESIGN.md` §3-1 의 v0.1 범위부터 만든다.
- 저장소는 public 이다(D-9). **PyPI 배포와 외부 홍보는 보류한다**(D-4). 공개 저장소이므로 비밀값·내부 자격증명을 커밋하지 않는다.

## 작업 방식
- **이슈 먼저.** 구현 전에 GitHub 이슈가 있어야 한다. 진행 상황(착수, 리뷰 결과, 반영·기각 사유, 막힘, 완료)은 그 이슈의 코멘트로 남긴다. 브랜치는 `issue-<N>-<slug>`, PR 본문에 `Closes #N` 을 쓴다.
- **메인 Claude 세션은 오케스트레이터다.** 이슈·브리프 작성, 위임, 결과 검증(테스트·CI 직접 실행), 이슈 기록, 병합을 맡는다. 구현 코드를 직접 쓰지 않는다.
- **구현**: herdr pane 의 Claude Code 를 **`cc-alt`**(두 번째 계정, `--dangerously-skip-permissions`)로 띄운다. `herdr agent start` 는 `claude` 바이너리를 직접 불러 alias 가 닿지 않으므로, 셸 pane 에서 `herdr pane run <id> "cc-alt --model claude-opus-5-5 --effort medium"` 로 띄운 뒤 `herdr agent rename <id> impl` 로 이름을 붙인다. 이슈가 바뀌면 문맥을 비우기 위해 새로 띄운다(종료는 `herdr agent send-keys impl ctrl+c ctrl+c` — 슬래시 명령은 prompt 로 보내면 실행되지 않는다).
- **리뷰**: herdr pane 의 Codex — `herdr agent start review --kind codex --pane <id> -- -m gpt-6-astra -c model_reasoning_effort=medium --sandbox workspace-write`(결과 파일을 써야 하므로. 쓰기는 `.work/` 아래만 허용한다고 브리프에 적는다). 지적은 구현자에게 돌려 고친다. 리뷰가 통과하고 CI 가 초록일 때만 병합한다.
- **README 는 활용 가이드다**(#11). 기능을 추가하는 PR 은 README 의 해당 절(설치·빠른 시작·사용 예·함정)을 함께 갱신한다. 없는 기능은 '예정'으로만 적는다.
- 브리프와 리뷰 결과는 `.work/issue-<N>/`(git 무시)의 파일로 주고받는다. 긴 지시를 프롬프트에 넣지 않는다.

## 규약
- 문서는 한국어, 커밋 메시지는 영어 Conventional Commits 로 쓴다.
- `django_sqlite_ops/boot/` 는 **Django 를 import 하지 않는다.** 복원 전에 DB 파일이 생기는 것을 막기 위해서다(DESIGN §4-1). 이 규칙은 테스트로 지킨다: `import django` 를 막은 상태에서 boot 를 import 해 본다.
- `apps.py` 의 `ready()` 와 시스템 체크는 DB 를 열지 않는다. 실제 연결은 `sqlite_doctor` 에서만 한다.
- **외부 도구의 설치법·명령·옵션은 공식 문서나 설치된 바이너리의 `-h` 로 확인한 뒤 쓴다.** 기억으로 안내하지 않는다. 예: Litestream 의 macOS 설치는 `brew install benbjohnson/litestream/litestream` 이다(https://litestream.io/install/mac/).
- 판정할 수 없으면 거부한다. 데이터를 지우거나 덮는 경로는 명시적 옵션 뒤에만 둔다.
- **결함은 재현한 뒤에 말한다.** "재현함 / 코드상 확인 / 미검증"을 구분해서 쓴다. 수정은 "수정 전에 실패하는 테스트"로 증명한다.
- **남의 저장소에는 아무것도 쓰지 않는다.** channels-lite, dj-lite, Litestream 을 비롯한 외부 저장소에 이슈·PR·코멘트를 만들지 않는다(D-5). 외부 결함은 `compat/` 의 버전 게이트 몽키패치로 다룬다.
- channels-nats 와 django-wireview 는 같은 조직 저장소지만 이 작업에서는 고치지 않는다. 필요한 변경은 OSS PO(마르코) 이슈로 넘긴다.
- 권장 설정은 `database.py` 의 표 하나가 정본이다. dj-lite 에 의존하지 않는다(D-10). 기본으로 켜는 값은 실측 근거가 있어야 한다.
- 의존성은 Django 만 필수다. channels-nats(`[nats]`)와 channels-lite(`[channels-lite]`)는 extra 로 둔다.

## CI
- GitHub Actions(`.github/workflows/ci.yml`)가 정본이다: lint(ruff) + Python 3.13/3.14 × Django 5.2/6.1 매트릭스 + `sqlite-floor`(Ubuntu 22.04 시스템 SQLite 3.37.2). uv 의 Python 은 최신 SQLite 를 품고 있어 하한은 `sqlite-floor` 에서만 검사된다.
- 로컬에서는 `scripts/ci-local.sh` 로 같은 워크플로를 act 로 돌린다(`lint`, `test <py> <django>` 로 좁힐 수 있다). 지원 범위를 바꾸면 이 매트릭스와 DESIGN §5 를 함께 고친다.
- act 는 Linux 컨테이너만 돌린다. macOS·Windows 잡은 로컬에서 재현되지 않는다.

## 함정 (실측에서 나온 것)
- Litestream endpoint 에 `http://` 가 빠지면 HTTPS 로 접속해 restore 가 무한 대기한다.
- `litestream ltx` 는 기본으로 L0 만 나열한다. 최대 TXID 를 볼 때는 `-level all` 을 쓴다.
- S3 가 끊겨도 Litestream 은 로그·`status`·메트릭에 아무것도 남기지 않는다(D3). 그래서 헬스는 TXID 를 직접 비교해 계산한다.
- Linux 에서 litestream-vfs 확장을 로드하면 그 뒤의 일반 connect 가 실패한다. 우회책 `sqlite3_reset_auto_extension()` 은 **모든** 자동 확장을 해제하므로 옵션으로만 둔다.
- SeaweedFS 는 버킷마다 볼륨을 잡는다. 랩에서는 버킷 하나를 두고 prefix 로 나누며, `-volume.max` 를 올린다.
- macOS 에는 `timeout` 명령이 없다.
- aio 계열 채널 레이어는 `await layer.close()` 가 없으면 프로세스가 끝나지 않을 수 있다.
