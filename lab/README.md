# 회귀 랩 (#10)

Docker 로 Litestream·boot·헬스의 실패 경계(L1–L8, [DESIGN §10](../docs/DESIGN.md#10-회귀-랩-실패-경계))를 재현하고, 배포 프로필 문서의 Dockerfile·compose 를 그대로 띄워 종단 검증한다. PRAGMA 후보 벤치도 여기서 돈다. 결과는 `docs/research/lab-<날짜>.md` 에 적는다.

기본 `pytest` 와 CI 에는 들어가지 않는다. `RUN_LAB=1` 일 때만 수집되고(`lab/conftest.py`), 실행 입구는 `scripts/lab.sh` 하나다.

```bash
scripts/lab.sh run all        # L1–L8, P1·P2 (약 8분). 끝나면 성공·실패와 관계없이 down -v
scripts/lab.sh run L6         # pytest -k 로 좁힌다 (L1..L8, P1, P2)
scripts/lab.sh bench          # PRAGMA 벤치 (기본 반복 5 × 변형 6, 약 25분)
scripts/lab.sh up             # 이미지 빌드 + SeaweedFS·toxiproxy 만 띄워 둔다(디버깅용)
scripts/lab.sh down           # 이 랩의 자원만 지우고 남은 것을 보여 준다(비어 있어야 한다)
```

### GitHub 러너(amd64)에서 돌리기

`.github/workflows/lab.yml` 은 `workflow_dispatch` 전용 잡이다(#25). 기본 CI(push/PR)에는 들어가지 않는다.

```bash
gh workflow run lab.yml -f scenarios=all    # 또는 P1, P2, L1..L8 (pytest -k 로 좁힌다)
gh run watch                                # 진행 보기
gh run download <run-id>                    # artifact lab-<선택>-<run-id> = lab/.out/ 전체
```

ubuntu-latest(amd64)에서 `scripts/lab.sh run <선택>` 을 그대로 돌리고, 성공·실패와 관계없이 `lab/.out/`(pytest 로그·`results-*.jsonl`·콘솔 `lab-run-<선택>.console`)을 artifact 로 올린다. 시작할 때 러너의 아키텍처·Docker·Compose 판과, 고정한 이미지 digest·`python:3.13-slim` 의 플랫폼 목록(`docker buildx imagetools inspect`)을 로그에 찍는다. 타임아웃 45분. 벤치는 공유 러너의 소음이 커서 넣지 않았다. act(`scripts/ci-local.sh`)는 Docker 소켓을 넘기지 않으므로 이 잡은 로컬에서 act 로 돌릴 수 없다 — 로컬에서는 `scripts/lab.sh` 를 쓴다.

필요한 것: Docker(Compose v2.24+, `!override` 사용), `uv`. 로그·결과는 `lab/.out/`(git 무시): `run-<시각>.log`, `results-<시각>.jsonl`(시나리오별 결과·소요 시간·핵심 로그 줄), `bench-<시각>.json`.

## Docker 자원 규칙

같은 호스트에 다른 프로젝트 컨테이너가 돈다는 전제다.

- compose 프로젝트 이름은 `LAB_PROJECT`(기본 `dso-lab`, 다르게 하려면 `dso-lab-<suffix>`). P1·P2 는 `<이름>-p1`·`<이름>-p2` 로 따로 띄운다.
- 모든 서비스·볼륨·네트워크에 라벨 `io.itda.dso-lab=1` 을 붙인다. `compose run` 컨테이너도 서비스 라벨을 받는다.
- 정리는 `docker compose -p <우리 프로젝트> ... --profile app down -v --remove-orphans` 만 쓴다. prune·전체 대상 `rm` 은 쓰지 않는다. 컨테이너 하나를 죽일 때도 `docker compose kill` 이다.
- 호스트 포트는 `127.0.0.1` 의 임의 포트로만 연다(toxiproxy API, 앱 8000).
- **볼륨 함정(재현함, Compose 5.5.1)**: compose 는 어느 서비스도 쓰지 않거나 비활성 프로필 서비스만 쓰는 볼륨을 모델에서 빼고, `down -v` 도 그 볼륨을 지우지 않는다(`config --volumes` 에 없음). 그래서 풀 볼륨 `v01..v40` 을 모두 붙인 `volume-holder` 서비스(시작하지 않음)를 두고 `--profile app` 으로 내린다.
- 이미지 `dso-lab-app:<git sha>` 는 다음 실행의 캐시로 남는다. 지우려면 이름으로 `docker image rm dso-lab-app:<태그>`.

## 구성

| 서비스 | 이미지 | 역할 |
|---|---|---|
| `s3` | `chrislusf/seaweedfs:4.48@sha256:4e61d15f…` | S3 대체. 버킷 `dso-lab` 하나를 시나리오별 prefix 로 나눈다. `-volume.max=200` |
| `toxiproxy` | `ghcr.io/shopify/toxiproxy:2.12.0@sha256:9378ed52…` | 앱 ↔ S3. 프록시 `s3`(:18333, boot·replicate)와 `s3h`(:18334, L8b 의 헬스 조회만) |
| `labapp` / `labapp-nv` | `dso-lab-app:<태그>` | 시나리오 앱. 볼륨 `LAB_VOLUME` 있음 / 없음(무상태). 재시작하지 않는다(종료 코드를 본다) |
| `tool` | 같은 이미지 | 볼륨 없이 복제본을 `/tmp` 로 복원해 TXID·행 수·`integrity_check` 를 낸다 |
| `loadgen` | 같은 이미지 | 벤치 부하 발생기(`lab_tools/loadgen.py`) |
| P1 `app` | 같은 이미지 | **single-server 문서의 compose.yaml 그대로** + `profile-override.yaml` |
| P2 `app`·`nats` | 같은 이미지, `nats:2.15.0@sha256:cd3fcd4e…` | **multiproc 문서의 compose.yaml 그대로** + 두 override |

앱 이미지의 베이스는 문서 Dockerfile 의 `python:3.13-slim` 그대로다(digest 고정 안 함 — 문서를 바꾸지 않기 위해서). 실행 때 받은 digest 는 결과 문서에 적는다.

## 문서와 다른 점 (이것뿐이다)

빌드 컨텍스트 `lab/.build/` 는 `lab/build_context.py` 가 `docs/profiles/*.md` 의 코드 블록에서 만든다. 손으로 옮겨 적지 않으므로 문서가 바뀌면 랩도 바뀐다. 바꾸는 줄은 `SUBSTITUTIONS` 표 하나에 있고, 그 줄이 정확히 한 번 나오지 않으면 실패한다(`tests/test_lab.py` 가 기본 테스트에서 확인).

| 조각 | 문서 | 랩 |
|---|---|---|
| Dockerfile | `COPY requirements.txt .` | `COPY requirements.txt django_sqlite_ops-0.0.0-py3-none-any.whl ./` — 작업 트리에서 `uv build` 한 wheel 을 설치 전에 넣는다. **다른 줄은 같다** |
| `requirements.txt` | `django-sqlite-ops @ https://github.com/.../main.zip` | `django-sqlite-ops @ file:///app/<wheel>` |
| `litestream.yml` | `bucket: my-app-backups`, `path: app`, `endpoint: http://s3.internal:8333` | `bucket: dso-lab`, `path: ${LAB_PREFIX}`(Litestream 이 환경 변수를 펼친다), `endpoint: http://toxiproxy:18333` |
| `settings.py`·`urls.py` | 조각 | 조각 그대로 + `lab/app/proj/*_tail.py`(SECRET_KEY·ALLOWED_HOSTS, `notes` 앱, 헬스 주기·grace·조회 설정, 벤치용 PRAGMA, 랩 뷰) |
| `entrypoint.sh` | 그대로 | 그대로(multiproc 판은 `/app/entrypoint-multiproc.sh` 로 넣고 P2 가 entrypoint 로 쓴다) |
| compose (P1·P2) | 그대로 | override: 이미지 이름, 라벨, 포트 `127.0.0.1::8000`(문서의 `8000:8000` 은 다른 서비스와 겹칠 수 있다), `LAB_PREFIX`·`BOOT_FLAGS` 주입. P2 는 `WEB_WORKERS=2`, nats 이미지 고정 |

P2 는 채널 레이어를 런타임에 쓰지 않는다(channels-nats 를 설치하지 않음). 설정을 읽는 `check --deploy`·`sqlite_doctor` 만 확인한다.

## 시나리오

| ID | 하는 일 | 확인 |
|---|---|---|
| P1 | single-server 문서 compose 로 빈 볼륨 소유자 → `check --deploy` → `--init-new` 첫 부팅 → doctor → 400건 쓰고 바로 `docker stop` → 같은 볼륨 재부팅 | 소유자 10001, `sqlite_ops.*` 없음, doctor exit 0, exit 0·복제 400/400, `match` |
| P2 | 위와 같고 uvicorn 워커 2 | 위 + 응답한 PID 2개, doctor 의 channels 줄 |
| L1 | 볼륨 A 로 200건 → 볼륨 없는 새 컨테이너 | `fresh` → 복원, 200건, 이어 쓴 것도 복제 |
| L2 | A 50건 → B 가 복원해 150건 → A 로 재부팅 | exit 2 `remote_ahead`, 복제본 TXID·150건 그대로, A 무변경 |
| L3 | 정상 종료 뒤 로컬 메타 디렉터리만 삭제 | exit 2 `no_local_meta`, DB 무변경 |
| L4 | toxiproxy `s3` 끔 / `timeout` toxic(무응답)+`--ltx-timeout 5` | 둘 다 exit 2 `remote_error`, 복구 후 `match` |
| L5 | S3 를 끊고 25건 → SIGKILL → S3 복구 → 재부팅 | `match`(`local_current`), 65건, 뒤이어 복제본 65건 |
| L6 | L2 의 옛 볼륨 + `--on-unknown restore`. rename 마다 잠드는 훅(`lab_hooks/sitecustomize.py`)으로 k 번째 rename 직후 SIGKILL, 모든 k 에 대해 → 훅 없이 재실행 | kill 직전 훅 로그의 rename 수가 정확히 k. 재실행 성공·150건, `.partial` 없음. kill 전 템플릿의 DB·`-wal`·`-shm`·메타 하위 전부가 **같은 하나의** 격리 디렉터리에 같은 해시로 있고 그 밖에는 manifest 뿐, 템플릿 파일 내용이 그 밖 어디에도 없음. 설치 뒤 kill(D-14)이면 격리 디렉터리 2개, 두 번째는 manifest·복원본 DB(+사이드카)만 (`_checks.quarantine_problems`) |
| L7 | 복제본의 모든 LTX 객체 가운데 64바이트를 뒤집음(filer API) | 새 컨테이너 exit 4 / 메타 삭제 볼륨 + `--on-unknown restore` exit 4, 로컬 파일 해시 무변경, 격리 없음 |
| L8a | 쓰기를 계속하며 `s3` 20초 끔(업로드·헬스 조회 모두) | 경과 시간 기준: 랩의 허용 시간(가정) `REFRESH × 3` + 진행 중 조회 여유(`REFRESH` 로 가정) + 표본 간격 + 1초 안에 `unknown`(`stale`/`remote_error`)로 바뀌고 단절이 끝날 때까지 유지. 전환 전 `caught_up` 은 age ≤ `REFRESH × 3`, `stale` 은 age > 그 값(응답 age 의 반올림 0.0005 허용, `_checks.full_outage_problems`). 복구 뒤 `caught_up`, 쓰기 오류 0 |
| L8b | 헬스 조회는 `s3h` 로 두고 업로드 경로 `s3` 만 20초 끔 | `backlog`(`local_ahead`) → 복구 뒤 `caught_up` |

L8 은 `LAB_HEALTH_REFRESH=2`, `LAB_HEALTH_GRACE=10` 으로 줄여 돈다(기본 15·60초면 20초 끊김이 grace 안에 든다).

L6 의 훅은 `LAB_RENAME_DELAY` 가 있을 때만 `os.rename` 뒤에 한 줄(`[lab-hook] rename k: ...`)을 남기고 잠든다. `PYTHONPATH=/app/lab_hooks` 로만 켜고 패키지 코드는 그대로다.

## PRAGMA 벤치

변형: 기준(권장 설정만), `temp_store=MEMORY`, `mmap_size=256MiB`, `cache_size=-65536`(64MiB), `journal_size_limit=64MiB`, 모두. 변형마다 새 볼륨·새 prefix 로 `--init-new` 부팅, `lab_seed` 로 같은 시드의 행 10만 개를 채우고, 5초 쉰 뒤 부하를 건다. Litestream 은 프로필 그대로 복제한다. 반복 r 마다 모든 변형을 돌고 순서를 r 만큼 회전한다(시간에 따른 호스트 부하 변화를 고르게 나눈다).

부하 발생기는 표준 라이브러리 Python(`lab_tools/loadgen.py`)이다. 같은 compose 네트워크의 별도 컨테이너에서 keep-alive 연결 16개로 닫힌 루프를 돈다. 요청 비율은 읽기(`/lab/read/<id>`) 70%, 정렬(인덱스 없는 열, 임시 B-트리) 10%, 쓰기(1행) 20%. 고른 이유: 이미지를 더 받지 않고, 요청 종류별 지연을 그대로 모으며, 1초마다 `/lab/stats` 로 `-wal` 크기를 표본할 수 있다. 병목이 앱·부하 발생기·호스트 중 어디인지와 부하 발생기의 여유는 재지 않았다(미검증). 그래서 벤치는 같은 조건에서 변형끼리의 상대 비교로만 읽는다.

판정: 같은 반복의 기준과 짝지어 차이(%)를 내고, **모든 반복에서 같은 방향으로 5% 이상**일 때만 '재현'으로 본다. `database.py` 기본값은 이 결과로 바꾸지 않는다(제안만).

## 파일

```
lab/
├── README.md               이 문서
├── build_context.py        문서 조각 → lab/.build/ (SUBSTITUTIONS)
├── lab-compose.yaml        기반 스택·시나리오 서비스·볼륨 풀
├── profile-override.yaml   P1·P2: 문서 compose 위에 덮는 것
├── multiproc-override.yaml P2 추가분
├── _lab.py                 compose·toxiproxy·조사 도구
├── _checks.py              L6·L8a 판정(순수 함수, tests/test_lab.py 가 Docker 없이 검사)
├── conftest.py             RUN_LAB 게이트, 결과 기록
├── test_scenarios.py       P1·P2, L1–L8
├── test_bench.py           PRAGMA 벤치
└── app/                    랩 Django 프로젝트(문서 조각 밖의 것)
    ├── manage.py, proj/asgi.py, proj/*_tail.py, notes/
    ├── lab_tools/          inspect_data.py, replica.py, s3_objects.py, loadgen.py
    └── lab_hooks/          sitecustomize.py (L6)
```
