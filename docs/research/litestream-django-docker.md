# Litestream × Django Docker 검증 (2부, 2026-10-07, 아이스버그)

1부(호스트 macOS + moto) 보고서는 `litestream-django-scenarios.md`입니다. 이 문서는 같은 질문을 **Linux 컨테이너와 장애 주입**으로 다시 돌린 결과입니다.

## 랩 구성
- **앱 이미지**: `python:3.13-slim`(Debian 13, 시스템 SQLite 3.46.1) + Django 6.1.2 + gunicorn + Litestream 0.5.17 linux-arm64(SHA256 확인) + litestream-vfs 0.5.17
- **S3**: SeaweedFS(`chrislusf/seaweedfs:latest`, S3 게이트웨이). MinIO 공식 이미지는 Docker Hub·quay 모두 익명 pull이 막혀 대체했습니다.
- **장애 주입**: toxiproxy 2.12.0. 앱과 S3 사이에 두고 연결 끊기와 지연을 넣었습니다.
- **엔트리포인트**: 1부 S2와 같습니다(`restore -if-db-not-exists -if-replica-exists` → `migrate` → `exec litestream replicate -exec gunicorn`). litestream이 PID 1입니다.
- **코드**: `~/Apps/django-lab/spikes/litestream/docker/` (`Dockerfile`, `compose.yaml`, `entrypoint.sh`, `guard.py`, `dlab.py`, `d0_smoke.py`, `d1_d2.py`, `d3_d5.py`, `d3b_d5.py`). 결과 로그: `docker/results.log`
- **정리**: 랩 컨테이너·볼륨·네트워크는 모두 지웠습니다(`docker compose -p djlab-ls down -v`, 라벨 `djlab=litestream`). 다른 프로젝트 컨테이너는 건드리지 않았습니다.

## 한눈 판정
- **D1a `docker stop`(SIGTERM → PID 1 litestream)**: 통과. 0.4초 만에 exit 0으로 내려갔고 400건 중 400건이 복제됐습니다.
- **D1b `docker kill`(SIGKILL), 쓰는 도중**: 통과. 응답한 1,600건이 로컬 볼륨과 복제본 양쪽에 모두 있었습니다. 같은 볼륨으로 다시 띄우면 이어서 서비스했습니다.
- **D1c 볼륨 없는 컨테이너 교체(무상태 배포)**: 통과. 새 컨테이너가 2.4초 만에 복원 후 200건 중 200건으로 떴습니다.
- **D2 S3 20초 단절**: 통과. 앱 쓰기 600건이 에러 0, p99 10.6ms였습니다. 복구 후 약 22초 안에 복제본이 따라잡았습니다(700/700).
- **D3 관측**: 위험. S3가 끊겨도 WARN/ERROR 로그 0줄, `sync_error_count` 메트릭 변화 0이었습니다. 운영자가 알아챌 수단이 기본으로는 없습니다.
- **D4 부팅 가드(1부 함정 B 방지)**: 통과. 가드가 없으면 최신본 150건이 55건으로 덮였고, 가드를 넣으면 150건에서 이어서 155건이 됐습니다.
- **D5 S3 지연 +40ms**: 주 DB 쪽은 통과. 쓰기 p50 2.3ms로 영향이 없었습니다(복제가 비동기라서).
- **D5 VFS 읽기 복제본**: 조건부. `CONN_MAX_AGE=0`이면 요청당 **1,008ms**, `None`이면 **1.7ms**였습니다.
- **D6 VFS 확장, Linux 컨테이너**: 틀림(우회 있음). 확장을 로드한 뒤 **일반 `sqlite3.connect()`까지 전부 실패**했습니다.

---

## D1. 신호와 컨테이너 수명
- **a) `docker stop`**: litestream이 SIGTERM을 받아 gunicorn에 넘기고 기다린 뒤 마지막 sync를 하고 종료했습니다(`litestream shut down`, 0.4초, exit 0). 복제본은 400/400입니다. PID 1을 litestream에 맡기는 구성이 성립하고, tini 같은 init가 따로 필요 없었습니다.
- **b) `docker kill`(SIGKILL), 4스레드로 쓰는 도중**: 응답한 1,600건이 로컬 볼륨과 복제본에 모두 있었습니다. 같은 볼륨으로 다시 띄우면 restore를 건너뛰고 1,600건으로 서비스했고, 복제도 그대로 이어졌습니다.
  - 1부 RPO 측정(약 1초 손실)과 다른 이유: 이번에는 앱 컨테이너만 죽었고 로컬 볼륨이 살아 있었습니다. 볼륨까지 잃는 경우(호스트 소실)가 1부의 상황입니다.
- **c) 볼륨 없는 컨테이너(Cloud Run·Fly 무상태 머신 식)**: 정상 종료 → 새 컨테이너가 2.4초 만에 복원하고 200/200으로 떴습니다.

## D2. S3 장애 동안 Django는?
- toxiproxy로 S3를 20초 끊었습니다.
  - 그동안 앱 쓰기 600건: 에러 0, 초당 1,469건, p50 2.2ms, p99 10.6ms. 복제가 비동기라 사용자 요청에는 영향이 없습니다.
  - WAL은 약 8.5MB까지 자랐습니다. 업로드하지 못한 변경이 쌓인 것입니다.
- S3를 되살리자 약 22초 안에 복제본이 700/700으로 따라잡았고 integrity ok였습니다.
- 의미: S3가 잠시 흔들려도 서비스는 멈추지 않습니다. 대신 그동안 쌓인 변경은 그 서버 디스크에만 있습니다. 이때 서버까지 죽으면 단절 시간 전체를 잃습니다.

## D3. 관측: 조용한 실패 (위험)
- 20초 단절 동안 확인한 것:
  - info와 debug 로그 모두 WARN/ERROR 0줄입니다. "error / fail / refused / retry / timeout"이 들어간 줄도 0이었습니다.
  - `litestream status`는 rc 0으로 헤더만 출력했습니다.
  - Prometheus 메트릭(`addr: ":9090"`)에서 `litestream_sync_error_count`는 변화가 없었습니다. 바뀐 것은 `sync_count`(로컬 WAL→LTX, 4→24)와 `txid`뿐이었습니다. `replica_operation_total{operation="PUT"}`는 단절 동안 늘지 않았습니다.
- 해석: 로컬 sync는 계속 성공하고, S3 업로드 실패는 내부 재시도로 숨겨집니다. 0.5.17 기준으로는 "복제본이 뒤처졌다"는 신호를 직접 주는 지표가 없습니다.
- Django 운영 대응:
  - 감시는 외부에서 합니다. 주기적으로 `litestream ltx <url>`의 최대 TXID나 마지막 파일 시각을 로컬 `litestream_txid`와 비교하거나, `replica_operation_total{PUT}`이 N분 동안 늘지 않으면 알람을 겁니다.
  - Django 헬스체크 뷰에 "마지막 복제 시각"을 넣는 것도 방법입니다. 가드 스크립트와 같은 방식으로 버킷의 최신 LTX 시각을 봅니다.
- [확인 필요] 단절 시간이 더 길면(분 단위) 로그 레벨이 올라가는지는 20초 단절로는 보지 못했습니다.

## D4. 부팅 가드: 옛 볼륨 재부팅이 최신본을 덮는 문제 막기
- 재현(Docker 볼륨 2개):
  1. 볼륨 A로 50건을 쓰고 정지합니다.
  2. 볼륨 B로 이어받아 150건까지 쓰고 정지합니다.
  3. **볼륨 A로 다시 부팅**합니다.
- **GUARD=0(기존 엔트리포인트)**: A가 옛 50건으로 서비스했고, 복제본 최신본이 **55건으로 덮였습니다**. 1부와 같은 결과입니다.
- **GUARD=1(`guard.py`)**: 로그가 `local_txid=1 remote_txid=3` → `local is behind → quarantined`였습니다. 로컬 DB를 `.stale-<시각>`으로 옮겨 두고 복원해 **150건으로 서비스했고, 이어서 쓴 결과는 155건**입니다.
- 가드의 원리(약 40줄, 의존성 없음):
  1. 로컬 메타 디렉터리(`.<db>-litestream/`)의 LTX 파일명에서 로컬이 마지막으로 복제한 TXID를 읽습니다.
  2. `litestream ltx <url>`에서 복제본의 최대 TXID를 읽습니다.
  3. 복제본이 앞서 있으면 로컬을 격리하고 복원하게 둡니다. 로컬 메타를 읽을 수 없으면 보수적으로 복원 쪽을 택합니다. 복제본에 연결할 수 없으면 로컬을 유지합니다.
- 한계(스파이크 수준):
  - 메타 디렉터리 구조는 Litestream 내부 구현이라 버전이 바뀌면 깨질 수 있습니다.
  - 두 머신이 **동시에** 쓰는 경우는 막지 못합니다. 그건 "쓰는 머신 1대"라는 배포 규칙의 몫입니다.
  - 그래도 "볼륨이 남는 플랫폼에서 재부팅"이라는 가장 흔한 사고는 막습니다.

## D5. S3가 멀 때(왕복 +40ms, 리전 간 거리 가정)
- **주 DB**: 쓰기 600건, p50 2.3ms, p99 64ms. 복제가 비동기라 Django 응답에는 거의 영향이 없습니다.
- **VFS 읽기 복제본**:
  - `CONN_MAX_AGE=0`(Django 기본): 요청당 **1,008ms**, 쿼리 p50 1,005ms. 요청마다 연결을 열 때 LTX 인덱스를 S3에서 다시 읽기 때문입니다. 1부 로컬 moto에서 32~48ms이던 것이 지연이 붙자 1초가 됐습니다.
  - `CONN_MAX_AGE=None`: 요청당 **1.7ms**, 쿼리 p50 0.34ms. 페이지 캐시와 인덱스가 연결에 남습니다.
  - 컨테이너 부팅(첫 연결): `None`일 때 2.3초.
- 판정: Django에서 VFS 복제본을 쓸 때 `CONN_MAX_AGE=None`은 권장이 아니라 **필수**입니다. 빠뜨리면 실제 S3에서는 요청마다 1초 안팎이 걸립니다.

## D6. VFS 확장이 Linux에서 일반 연결을 망가뜨림 (틀림, 우회 있음)
- 증상: `python:3.13-slim`에서 `litestream_vfs`를 한 번 `load_extension`하면, 그 뒤 **모든** `sqlite3.connect()`(`:memory:`나 일반 파일도)가 `sqlite3.DatabaseError: automatic extension loading failed`로 실패했습니다. Django는 default DB 연결부터 죽습니다. macOS(1부)에서는 나지 않았습니다.
- 원인: 확장이 VFS 말고 auto-extension도 등록하는데, VFS가 아닌 연결에서 실패 코드를 돌려줍니다. 상류에 같은 내용의 PR이 열려 있습니다: benbjohnson/litestream #1506 "fix(vfs): return SQLITE_OK from the auto-extension for non-VFS connections"(2026-09-03, 미머지).
- 우회(검증함):
  - 확장을 로드한 직후 `ctypes.CDLL(_sqlite3.__file__).sqlite3_reset_auto_extension()`을 호출합니다. 그러면 auto-extension만 풀리고 VFS 등록은 유지됩니다.
  - 확인한 것: `sqlite3_vfs_find("litestream")`가 참이고, 일반 파일 연결이 정상이며, D5의 VFS 복제본 읽기도 정상(1,200건)이었습니다.
  - 코드는 `docker/app/config/vfs.py`에 있습니다.
- 판정: #1506이 머지되기 전까지 Linux에서 Django + VFS를 쓰려면 이 우회가 필수입니다. 다만 우회는 내부 동작에 기대므로 버전을 올릴 때마다 다시 확인해야 합니다.

---

## 1부 판정 갱신
- **S2 컨테이너 엔트리포인트**: "통과(함정 둘)"에서 "통과, 실제 컨테이너에서 확인"으로 바꿉니다. PID 1 litestream, `docker stop`·`kill`, 무상태 교체 모두 확인했습니다.
- **함정 B(옛 로컬 DB 덮어쓰기)**: "런북 대응"에서 "부팅 가드로 차단 가능(D4)"으로 바꿉니다.
- **S6 VFS 읽기 복제본**: "조건부"를 유지하되 조건이 늘었습니다. `CONN_MAX_AGE=None` 필수(지연이 붙으면 0일 때 1초), Linux에서 auto-extension 우회 필수(D6).
- **새 위험**: 복제 실패가 로그·메트릭에 드러나지 않습니다(D3). 외부 감시가 필수입니다.
- **S7 Writable VFS**: 틀림 그대로입니다. 컨테이너에서는 다시 돌리지 않았습니다.

## Django 실무용 체크리스트 (이번 검증 기준)
1. settings: `transaction_mode="IMMEDIATE"`, `init_command`로 WAL·`synchronous=NORMAL`·`busy_timeout`을 넣습니다. 새 NOT NULL 필드에는 `db_default`를 씁니다.
2. 엔트리포인트: guard → `restore -if-db-not-exists -if-replica-exists` → `migrate` → `exec litestream replicate -exec "gunicorn …"`. litestream을 PID 1로 둡니다.
3. 쓰는 컨테이너는 1개입니다. 배포는 정지 후 기동 방식으로 하고, 블루/그린은 쓰지 않습니다.
4. 복제 지연은 외부에서 감시합니다(버킷의 최신 LTX 시각, PUT 카운터).
5. 읽기 복제본은 follow 파일(`restore -f`)이 무난합니다. VFS는 `CONN_MAX_AGE=None`과 Linux 우회가 전제입니다.
6. Writable VFS를 기본 DB로 쓰지 않습니다.

## 미검증 / 남은 일
- 실제 클라우드 S3·R2·Tigris(비용, 실제 지연, 일관성 모델): 클라우드 버킷이 필요해 마스터 승인이 필요합니다.
- 분 단위 이상 S3 단절에서의 로그·메트릭 변화
- compose 운영 관점(헬스체크, 재시작 정책, 리소스 제한) 감수: 징베에게 넘길 수 있습니다.
- #1506(VFS auto-extension), #1271/#1363(writable VFS 손상)이 머지되면 D6·S7 재검증
