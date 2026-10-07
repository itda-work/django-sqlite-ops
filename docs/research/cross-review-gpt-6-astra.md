## 요약 판정 (3줄)

**[판단] 운영 패키지라는 방향에는 동의하지만, 현재 가드를 자동 복원 기능으로 공개하는 것은 반대합니다.** 메타 부재와 조회 실패를 안전하게 구분하지 못합니다. [guard.py:19](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:19)
**[판단] 자체 채널 레이어 보류는 타당하지만, “50ms는 구조적 하한”이라는 근거는 틀렸습니다.** 폴링 주기는 설정값입니다. [layers/__init__.py:47](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/__init__.py:47)
**[판단] v0.1은 진단·명시적 복원·기동·복제 상태 관측으로 좁히고, VFS 강제 설정과 자동 토폴로지 판정은 제외해야 합니다.** 현재 제안은 확인 가능한 사실보다 강한 보장을 약속합니다. [설계:74](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:74)

## 질문별 답 (1~5)

아래 **[사실]**은 코드·로그·명시한 상류 자료에서 확인한 내용, **[추론]**은 그로부터 도출한 실패 경로, **[판단]**은 설계 제안입니다. 파일 변경이나 벤치마크 재실행은 하지 않았습니다.

### 1. 운영 패키지라는 경계가 맞는가?

**[판단] 맞습니다. 다만 “자동으로 최신 DB를 선택하는 패키지”까지 포함하면 현재 근거로는 감당할 수 없습니다.** dj-lite는 이미 표준 `DATABASES` 딕셔너리를 반환하고, Django가 `transaction_mode`와 `init_command`를 처리합니다. 새 설정 생성기나 별도 호환 API를 만들 이유는 약합니다. 표준 Django 설정을 읽으면 dj-lite 결과도 자연스럽게 지원됩니다. [configurator.py:106](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/dj-lite/src/dj_lite/configurator.py:106), [SQLite base.py:180](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/base.py:180)

**[판단] v0.1 범위는 다음처럼 줄이는 편이 좋습니다.**

| 유지·추가 | 제외·연기 |
|---|---|
| 정적 설정 검사와 명시적으로 실행하는 실제 DB 진단을 분리. 연결을 열면 Django가 `init_command`까지 실행하므로 검사가 상태를 바꿀 수 있습니다. [base.py:203](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/base.py:203) | 자체 설정 생성기. dj-lite가 반환하는 표준 딕셔너리 지원으로 충분합니다. [configurator.py:106](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/dj-lite/src/dj_lite/configurator.py:106) |
| `guard`는 우선 **읽기 전용 진단과 모호한 상태에서의 기동 거부**. 복원은 명시적인 정책과 검증된 대상이 있을 때만 수행합니다. [guard.py:25](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:25) | TXID 비교만으로 로컬 DB를 격리하는 기본 동작. 현재 분기는 데이터의 포함 관계를 검사하지 않습니다. [guard.py:34](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:34) |
| 복원 계획·무결성 확인, 복원 실패 후 재시작 정책, 격리본 복구 절차. `ltx` 목록의 최대 TXID만으로 복원 가능성을 보장할 수 없습니다. [Litestream restore](https://litestream.io/reference/restore/) | VFS 라우터·전역 확장 우회·테넌트 관리. 이미 2단계로 미루는 방향이 적절합니다. [설계:121](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:121) |
| 단일 쓰기 주체라는 배포 계약과 기동 중 로컬 배타 잠금. 다중 노드의 배타성은 외부 배포 시스템의 책임으로 명시합니다. [Docker 보고서:61](/Users/allieus/hermes-outbox/2026-10-07/litestream-django-docker.md:61) | NATS 서버 감독과 NATS 인증 정책 검사. SQLite 운영 범위를 벗어나며 기존 레이어의 연결 설정과 책임이 겹칩니다. [설계:108](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:108), [layer.py:484](/Users/allieus/Apps/itda-work/channels-nats/channels_nats/layer.py:484) |

**[사실·추론] `manage.py sqlite_serve`를 셸 엔트리포인트의 단순 이식으로 보면 안 됩니다.** Django는 명령 실행 전에 `django.setup()`을 하고, 일반적인 management command는 `handle()` 전에 system checks도 수행합니다. DB를 여는 검사나 초기화 코드가 있으면 guard 전에 DB가 생성·변경되거나 열린 연결이 남을 수 있습니다. **[판단] 복원 전 단계는 Django 초기화 이전의 독립 CLI로 두거나, 최소한 이 순서를 설계 계약으로 해결해야 합니다.** [management/__init__.py:415](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/core/management/__init__.py:415), [management/base.py:461](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/core/management/base.py:461)

**[판단] 헬스 기능은 필요하지만 “최신 LTX 시각 = 복제 지연”으로 정의하면 안 됩니다.** 쓰기가 없는 정상 DB도 오래된 업로드 시각을 가질 수 있고, 로컬 LTX TXID에는 아직 LTX로 옮기지 않은 SQLite 커밋이 반영되지 않습니다. `caught_up / backlog / unknown`과 관측 시각을 분리하고, 실제 복구 가능성은 별도 복원 검증으로 확인해야 합니다. D3는 외부 관측 필요성의 근거이지 시간 기반 지표의 정확성까지 검증한 것은 아닙니다. [Docker 보고서:39](/Users/allieus/hermes-outbox/2026-10-07/litestream-django-docker.md:39), [Litestream v0.5.17 db.go](https://raw.githubusercontent.com/benbjohnson/litestream/v0.5.17/db.go)

### 2. stale-local 판정은 sound한가?

**[판단] 아닙니다.** 성립하려면 같은 복제 이력, DB와 메타의 일치, 미반영 로컬 커밋 부재, 단일 쓰기 주체, 성공적이고 충분한 원격 조회가 필요합니다. 현재 코드는 파일명 숫자만 비교합니다. D4가 확인한 것은 두 볼륨을 순차 전환한 `local=1, remote=3` 사례뿐입니다. [guard.py:19](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:19), [d3_d5.py:45](/Users/allieus/Apps/django-lab/spikes/litestream/docker/d3_d5.py:45)

| 조건 | 확인된 동작과 실패 경로 |
|---|---|
| **원격 조회 실패** | **[사실]** 오류와 빈 목록을 모두 `keep local`, exit 0으로 처리합니다. **[추론]** 옛 볼륨이 S3 단절 중 부팅하면 검증 없이 서비스하고, 나중에 복제가 재개되어 최신 원격 이력을 훼손할 수 있습니다. 타임아웃도 없습니다. [guard.py:19](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:19), [entrypoint.sh:14](/Users/allieus/Apps/django-lab/spikes/litestream/docker/entrypoint.sh:14) |
| **메타 없음·경로 다름** | **[사실]** 기본 메타 경로만 검색하고 `local is None`이면 복원합니다. **[추론]** 실제 DB에 원격보다 새로운 커밋이 있어도 메타 삭제·분리 마운트·사용자 지정 `meta-dir` 때문에 **새 로컬 DB를 잘못 포기**합니다. 공식 `reset`도 DB를 유지하면서 메타를 제거하는 정상 작업입니다. [guard.py:25](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:25), [reset 문서](https://litestream.io/reference/reset/), [1부 보고서:67](/Users/allieus/hermes-outbox/2026-10-07/litestream-django-scenarios.md:67) |
| **DB와 메타 불일치** | **[사실]** 파일 내용·DB 식별자·WAL을 확인하지 않습니다. **[추론]** 옛 DB만 되돌리고 높은 TXID의 메타를 남기면 `local >= remote`가 되어 **오래된 DB를 유지**합니다. 반대로 오래된 메타에 새 DB가 붙으면 잘못 격리합니다. [guard.py:27](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:27) |
| **LTX에 아직 반영되지 않은 쓰기** | **[추론]** 로컬 LTX가 10이고 원격이 11이더라도 로컬 DB/WAL에 별도의 최신 커밋이 있을 수 있습니다. 이때 remote가 더 크다는 이유로 복원하면 해당 커밋을 서비스 DB에서 잃습니다. 원격의 숫자가 크다는 사실은 로컬 데이터 전체를 포함한다는 뜻이 아닙니다. [guard.py:34](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:34), [Docker 보고서:43](/Users/allieus/hermes-outbox/2026-10-07/litestream-django-docker.md:43) |
| **두 쓰기 주체·분기된 이력** | **[추론]** 서로 다른 변경을 가진 두 DB가 같은 TXID에 도달하거나 오래된 분기가 더 높은 TXID에 도달하면 stale DB가 통과합니다. 반대로 원격 TXID가 더 크면 로컬에만 있는 변경을 버립니다. 조회 후 다른 노드가 쓰는 경쟁도 막지 못합니다. 두 노드 동시 복제의 덮어쓰기는 기존 보고서에도 있습니다. [guard.py:34](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:34), [1부 보고서:49](/Users/allieus/hermes-outbox/2026-10-07/litestream-django-scenarios.md:49) |
| **압축·보존·스냅샷만 남은 복제본** | **[사실]** 현재 `litestream ltx URL`은 기본 L0만 조회합니다. 다만 **0.5.17의 정상 L0 보존 정리는 최신 L0를 남기므로, 정상 압축만으로 반드시 오판한다고 할 수는 없습니다.** 외부 lifecycle·부분 복사 등으로 L0만 없어지고 상위 레벨에 백업이 남은 경우에는 빈 복제본으로 오판합니다. 해당 조건의 실환경 재현은 **[확인 필요]**입니다. [guard.py:19](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:19), [v0.5.17 ltx.go](https://raw.githubusercontent.com/benbjohnson/litestream/v0.5.17/cmd/litestream/ltx.go), [v0.5.17 db.go](https://raw.githubusercontent.com/benbjohnson/litestream/v0.5.17/db.go) |
| **시계 변경** | **[사실]** stale 판정에는 시각을 사용하지 않으므로 시계 오차가 TXID 대소 비교를 직접 뒤집지는 않습니다. 격리 이름은 초 단위 `time.time()`이므로 같은 초 재시도·시계 역행에 이름 충돌 위험이 있습니다. 시간 기반 health에도 별도 시계 계약이 필요합니다. [guard.py:35](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:35), [설계:84](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:84) |

**세대·스냅샷에 대한 중요한 구분:** **[사실]** 0.5.17은 스냅샷 생성 때도 다음 TXID를 `pos.TXID + 1`로 정합니다. 로컬이 원격보다 뒤면 원격 최신 L0를 로컬 기준점으로 가져온 뒤 현재 DB의 스냅샷을 만들도록 합니다. 따라서 **“새 스냅샷이면 무조건 TXID가 1로 재설정된다”는 주장은 틀립니다.** 다만 이력을 지우고 새 복제 대상으로 시작하는 경우에는 이전 숫자와의 비교 자체가 무의미합니다. 이전 이력의 local=100과 새 이력의 remote=3을 비교해 로컬을 유지하는 조건부 반례는 성립하며, 구체적인 재설정 경로별 재현은 **[확인 필요]**입니다. [v0.5.17 db.go, `checkDatabaseBehindReplica`·`sync`](https://raw.githubusercontent.com/benbjohnson/litestream/v0.5.17/db.go)

**[사실·추론] 격리 자체도 복구 트랜잭션이 아닙니다.** `db.name + "*"`는 WAL·SHM 외에 같은 접두사의 다른 파일까지 옮기고, DB·WAL·메타를 여러 번의 rename으로 이동합니다. 중간 종료 시 부분 이동 상태가 남을 수 있으며, 재시작은 DB 존재 여부만으로 경로를 선택합니다. 복원 검증도 격리 이후입니다. **[판단] 정확한 대상 목록, 배타 잠금, 충돌 없는 격리 식별자, 단계별 재시작·롤백 규약이 필요합니다.** [guard.py:16](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:16), [guard.py:35](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:35), [entrypoint.sh:17](/Users/allieus/Apps/django-lab/spikes/litestream/docker/entrypoint.sh:17)

격리본을 남기므로 위 오판이 곧바로 **물리적 영구 삭제**를 뜻하지는 않습니다. 그러나 서비스 DB에서 커밋이 사라지고 이후 복제도 잘못된 상태로 진행될 수 있으므로, “보수적 복원”이라고 부를 수 없습니다. [guard.py:35](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:35)

### 3. 벤치마크는 공정한가? 자체 레이어 보류의 근거가 되는가?

**[판단] “이 설정에서 이 구현들의 저부하 지연 비교”로는 유효합니다. 튜닝 한계·최대 처리량·일반적 신뢰성 비교로 확장하면 안 됩니다.** 기록된 0.3ms, 51.8ms, 53.3ms와 fan-out 결과는 설계 문서와 일치합니다. [verify2.log:4](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/verify2.log:4)

- **폴링 기본값:** **[사실]** 벤치는 `polling_interval`을 지정하지 않아 0.1초를 사용합니다. 빈 큐에서 그만큼 쉽니다. **[추론]** 메시지 도착 위상이 고르게 분포하면 대기 중앙값 약 50ms는 자연스럽습니다. **[판단]** 1/5/10/100ms 튜닝과 CPU·쿼리량 비교 없이 “튜닝으로 없어지지 않는 하한”이라고 해서는 안 됩니다. [settings_bench.py:19](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/settings_bench.py:19), [layers/__init__.py:319](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/__init__.py:319)

- **spawn·초기화:** **[사실]** 송신 시각은 자식 프로세스 안에서 찍으므로 spawn 시간 자체는 A/B 지연에 포함되지 않습니다. 수신 준비 후 대기도 있습니다. 반면 첫 송신의 지연 연결·풀 초기화는 포함될 수 있고, 준비 신호 일부는 receive 태스크가 실제 실행되기 전에 보냅니다. **[판단]** “spawn 때문에 SQLite가 50ms 느리다”는 설명은 부정확하지만, warm-up과 실제 구독 준비 barrier는 필요합니다. [bench.py:45](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:45), [bench.py:64](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:64), [bench.py:136](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:136)

- **sleep pacing:** **[사실]** A는 `send()` 완료 후 5ms, B는 `group_send()` 완료 후 300ms를 쉽니다. A는 수신 확인을 기다리는 왕복 순차 시험이 아닙니다. **[추론]** 구현별 송신 비용에 따라 실제 제공 부하가 달라지고, B는 매 회차 사이에 큐를 비울 시간을 줍니다. 포화 상태의 차이를 과소평가하고 폴링 위상에 결과가 좌우될 수 있습니다. [bench.py:48](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:48), [bench.py:82](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:82)

- **시계·집계:** **[사실]** 송수신 모두 `time.time()`을 씁니다. 같은 호스트이므로 호스트 간 시차 문제는 없지만 벽시계 보정에는 취약합니다. B의 `full`에는 도착 수가 200인지 검사하지 않은 회차도 들어갑니다. **[판단]** 공통 기준의 monotonic 시계와 회차별 고유 멤버 수 검증이 필요합니다. 이번 로그의 4000/4000은 사실이나 하네스 일반의 “all-arrive” 보장은 아닙니다. [bench.py:39](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:39), [bench.py:158](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:158)

- **설정 동등성:** **[사실]** aio는 사용자 `init_command`가 있으면 자체 cache/mmap/busy-timeout 기본 묶음을 대체하며, 연결 생성에 Django의 `transaction_mode`·`timeout`을 그대로 전달하지 않습니다. ORM은 기본 JSON, aio는 msgpack입니다. **[판단]** 완전히 동일한 튜닝 조건의 비교라고 표현해서는 안 됩니다. [aio.py:43](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/aio.py:43), [aio.py:82](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/aio.py:82), [core.py:12](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/core.py:12)

- **유휴 부하 결론은 과장:** **[사실]** C는 이벤트·멤버십을 지운 뒤 10초를 측정합니다. 200개 채널은 한 레이어의 같은 process prefix를 공유하고, 폴링 태스크도 prefix별로 하나입니다. 빈 큐에서도 확률적으로 cleanup을 실행하며 cleanup은 DELETE를 수행합니다. 따라서 “폴링은 읽기만 한다 → 유휴 복제 부담은 없다”는 일반화는 틀립니다. 또한 `data_version` 차이는 커밋 횟수 계측 API가 아닙니다. [bench.py:170](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:170), [layers/__init__.py:116](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/__init__.py:116), [layers/__init__.py:216](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/__init__.py:216), [core.py:69](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/core.py:69), [SQLite data_version](https://sqlite.org/pragma.html#pragma_data_version)

- **D2는 NATS 취소 의미론의 영향을 받음:** **[사실]** `wait_for(receive(), 0.5)`가 반복되고, NATS는 일반 채널의 마지막 receive가 취소되면 구독을 해제합니다. README도 이 사이 메시지 손실을 명시합니다. **[추론]** 196/200에는 이 공백이 영향을 줬을 수 있으며 정확한 4건의 원인은 **[확인 필요]**입니다. 모든 예외를 `ChannelFull`로 세는 코드와 수신자 사이 교집합만 검사하는 중복 집계도 고쳐야 합니다. [bench.py:110](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:110), [bench.py:129](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:129), [bench.py:195](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/bench.py:195), [layer.py:1144](/Users/allieus/Apps/itda-work/channels-nats/channels_nats/layer.py:1144), [README:80](/Users/allieus/Apps/itda-work/channels-nats/README.md:80)

**추가로 중요한 코드 결함:** **[사실]** aio의 선점 성공 판정은 UPDATE의 `rowcount`가 아니라 `conn.total_changes > 0`입니다. 이 값은 연결 생성 이후 누적 변경량입니다. **[추론]** 이전 쓰기가 있었던 연결 두 개가 같은 이벤트를 SELECT하면, UPDATE에서 진 쪽도 메시지를 반환할 수 있습니다. ORM 판은 해당 UPDATE 결과를 검사하므로 구분해야 합니다. 실제 경쟁 재현은 **[확인 필요]**이나 코드상 판정 오류는 명확합니다. [aio.py:164](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/aio.py:164), [core.py:44](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/core.py:44), [Python total_changes](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.total_changes)

**[판단] 자체 구현 보류에는 동의합니다.** 다만 근거는 “SQLite의 보편적 50ms 한계”가 아니라 기존 레이어의 수명·경쟁·전달 의미론까지 새로 책임져야 한다는 유지 비용입니다. 이번 결과는 채널 레이어 연구를 끝낼 증거가 아니라 v0.1 범위에서 제외할 근거입니다. [aio.py:234](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/aio.py:234), [channels-nats README:71](/Users/allieus/Apps/itda-work/channels-nats/README.md:71)

### 4. wireview 무변경·channels-nats 동결과 양립하는가?

**[사실] wireview의 transport 구현 변경은 필요 없어 보입니다.** `ChannelsBroker`는 `group_send`와 `send`, `ChannelsOutbound`는 `group_add`·`group_discard`에 위임합니다. SQLite/Litestream 의존은 없습니다. 다만 설계 문서의 “ChannelsBroker → group_add” 설명은 실제 책임 배치와 다릅니다. [transport.py:107](/Users/allieus/Apps/itda-work/django-wireview/wireview/core/transport.py:107), [transport.py:158](/Users/allieus/Apps/itda-work/django-wireview/wireview/core/transport.py:158)

**[판단] “transport 무변경”을 “통합 검증 불필요”로 확대하면 안 됩니다.** 랩 엔트리포인트는 `gunicorn config.wsgi`지만 wireview 배포는 ASGI를 요구합니다. 새 `serve`는 ASGI 명령 전달과 종료·재접속을 검증해야 합니다. 템플릿 플래그를 넣는다면 그것도 별도의 wireview 변경입니다. [entrypoint.sh:19](/Users/allieus/Apps/django-lab/spikes/litestream/docker/entrypoint.sh:19), [DEPLOYMENT.md:5](/Users/allieus/Apps/itda-work/django-wireview/docs/DEPLOYMENT.md:5), [설계:100](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:100)

**[사실] wireview W006은 실제 프로세스 수를 감지하지 않습니다.** 백엔드 문자열에 `InMemoryChannelLayer`가 있으면 항상 deploy warning을 냅니다. W012는 default 레이어 누락을 검사합니다. **[판단]** 새 패키지가 이 둘을 재구현하면 중복이며, 단일 프로세스 프로필에서도 W006이 나오는 현재 동작을 문서화해야 합니다. [checks.py:502](/Users/allieus/Apps/itda-work/django-wireview/wireview/checks.py:502), [checks.py:525](/Users/allieus/Apps/itda-work/django-wireview/wireview/checks.py:525), [checks.py:959](/Users/allieus/Apps/itda-work/django-wireview/wireview/checks.py:959)

**[판단] 기존 API를 사용하는 배포 프로필은 기능 동결과 양립합니다.** 반면 새 persistence·순서 보장·감독 API를 요구하면 범위를 벗어납니다. 특히 channels-nats README는 특별한 선택 이유가 없는 신규 프로젝트에는 channels_redis부터 검토하라고 합니다. 따라서 NATS를 **wireview 대상 프로필의 기본값**으로 삼는 것은 가능하지만, 범용 SQLite 패키지의 유일한 표준으로 올리는 것은 과합니다. wireview도 Redis를 지원한다고 명시합니다. [README:17](/Users/allieus/Apps/itda-work/channels-nats/README.md:17), [README:26](/Users/allieus/Apps/itda-work/channels-nats/README.md:26), [DEPLOYMENT.md:63](/Users/allieus/Apps/itda-work/django-wireview/docs/DEPLOYMENT.md:63)

**[사실·판단] wireview는 레이어가 알리지 않는 드롭을 복구하지 않습니다.** transport 주석도 이 한계를 인정합니다. NATS의 수신 전 메시지 손실을 “규약대로”라고만 기술하지 말고, 저장 위치·`ChannelFull`·취소 공백·그룹 순서의 차이를 프로필 계약으로 명시해야 합니다. [transport.py:85](/Users/allieus/Apps/itda-work/django-wireview/wireview/core/transport.py:85), [README:71](/Users/allieus/Apps/itda-work/channels-nats/README.md:71), [README:90](/Users/allieus/Apps/itda-work/channels-nats/README.md:90)

### 5. check 시점에 불가능하거나 불안정한 항목은?

**[판단] 정적 설정 오류, 현재 환경에서 관측한 상태, 배포자의 선언을 구분해야 합니다.** 다음 항목들을 같은 Error/Warning 검사 묶음으로 취급하면 오탐과 거짓 안전 판정이 생깁니다. [설계:74](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:74)

| 제안 검사 | 검증 한계와 권고 |
|---|---|
| `transaction_mode != IMMEDIATE` | **[사실]** Django는 DEFERRED·EXCLUSIVE·IMMEDIATE를 모두 허용합니다. **[판단]** 미설정을 일반 오류로 만들지 말고 쓰기 프로필의 권고로 제한하십시오. 실제 atomic 시작은 이 설정을 사용합니다. [base.py:143](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/base.py:143), [base.py:328](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/base.py:328) |
| `timeout` / `busy_timeout` 없음 | **[사실]** Python sqlite3 기본 timeout은 5초입니다. 키 부재는 대기 없음이 아닙니다. Django는 연결 후 `init_command`를 실행하므로 최종 유효값은 설정 키 검사만으로 보장되지 않습니다. **[판단]** “명시하지 않음”과 “실제 timeout=0”을 분리하십시오. [Python connect](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.connect), [base.py:203](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/base.py:203) |
| 실제 WAL 여부 | **[사실]** 연결을 열어 PRAGMA를 읽을 수 있지만, 그 연결 생성 자체가 WAL 설정을 실행할 수 있습니다. **[판단]** 정적 검사와 opt-in 연결 진단을 분리하고 메모리 DB·읽기 복제본을 제외하십시오. 전체 실행 기간의 WAL 상태 보장으로 표현하면 안 됩니다. [base.py:203](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/base.py:203), [SQLite WAL](https://www.sqlite.org/wal.html) |
| SQLite 버전 하한 | **[사실]** 현재 Python이 사용하는 버전은 검사 가능합니다. 설치된 Django는 이미 3.37 하한과 연결 시 검사를 갖습니다. **[판단]** 중복 하한보다 Litestream/VFS 조합의 추가 요구만 검사하고, check 실행 환경과 실제 worker 환경의 동일성은 별도 전제로 두십시오. [features.py:14](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/features.py:14), [base/base.py:190](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/db/backends/base/base.py:190) |
| 네트워크 파일시스템 | **[사실]** WAL의 공유 메모리 제약은 실제 위험입니다. **[판단]** OS별 마운트 정보로 알려진 NFS/SMB를 경고할 수는 있지만, 경로 문자열만으로 저장소 특성과 잠금 신뢰성을 인증할 수는 없습니다. 컨테이너·FUSE·다른 배포 호스트까지 포함한 탐지 정확도는 **[확인 필요]**이며 `unknown`을 허용해야 합니다. [SQLite WAL](https://www.sqlite.org/wal.html), [설계:76](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:76) |
| InMemory + 다중 프로세스 | **[사실]** wireview도 프로세스 수를 읽지 않습니다. **[판단]** 독립 `manage.py check`가 향후 worker·외부 publisher 수를 일반적으로 알아낼 수 있다는 계약은 불가능합니다. 명시적 배포 선언 또는 보편적 경고로 제한하십시오. [checks.py:502](/Users/allieus/Apps/itda-work/django-wireview/wireview/checks.py:502) |
| Litestream 설정 존재 | **[사실]** 랩은 entrypoint에서 `/etc/litestream.yml`을 생성합니다. **[추론]** 이미지 빌드 중 검사에는 없고 실행 시에는 있을 수 있습니다. 존재 자체로 daemon 실행·인증·업로드 성공은 증명되지 않습니다. **[판단]** 명시적 config 경로의 구문·DB 매핑 검증과 runtime probe를 분리하십시오. [entrypoint.sh:6](/Users/allieus/Apps/django-lab/spikes/litestream/docker/entrypoint.sh:6), [Docker 보고서:39](/Users/allieus/hermes-outbox/2026-10-07/litestream-django-docker.md:39) |
| 복제 대상의 일시 데이터 DB | **[판단]** DB 별칭만으로 데이터의 보존 정책을 추론하지 마십시오. 전용 채널 DB라는 명시적 역할과 실제 라우팅을 확인해야 합니다. ORM 레이어는 router를 통해 DB를 선택하므로 `CONFIG.database`만 검사하면 실제 저장 파일과 어긋날 수 있습니다. [core.py:26](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/core.py:26), [router.py:16](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/router.py:16) |
| NATS URL·토큰 | **[사실]** 인증·TLS 등을 `connect_options`로도 전달합니다. **[판단]** URL 안에 토큰이 없다는 이유로 경고하면 오탐입니다. 실제 인증 유효성은 연결 probe의 영역이며 SQLite 패키지의 필수 check에서 빼는 것이 좋습니다. [layer.py:215](/Users/allieus/Apps/itda-work/channels-nats/channels_nats/layer.py:215), [layer.py:526](/Users/allieus/Apps/itda-work/channels-nats/channels_nats/layer.py:526) |

**VFS의 `CONN_MAX_AGE` 강제는 별도 must-fix입니다.** **[사실]** D5는 WSGI/gunicorn에서 측정했고, Django 공식 async 문서는 영속 연결을 비활성화하라고 권고합니다. **[판단]** 이를 wireview ASGI 환경에 보편적으로 강제하면 안 됩니다. 해당 조합의 연결 수명과 지연은 **[확인 필요]**입니다. [d3_d5.py:72](/Users/allieus/Apps/django-lab/spikes/litestream/docker/d3_d5.py:72), [Django async ORM](https://docs.djangoproject.com/en/6.0/topics/async/#queries-the-orm)

## must-fix (설계를 바꿔야 하는 것)

1. **자동 최신본 선택을 v0.1의 안전성 보장에서 제거하십시오.** 메타 없음·원격 조회 실패·이력 불일치는 `unknown`으로 분류하고 기본 기동을 중단해야 합니다. TXID 대소 비교만으로 격리·복원하지 마십시오. [guard.py:19](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:19)

2. **복원과 기동을 재시작 가능한 상태 전이로 설계하십시오.** Django 초기화 이전 실행, 정확한 파일 집합, 배타 잠금, 복원 검증, 실패 후 복구 경로가 필요합니다. 현재 rename 루프와 `-if-db-not-exists` 조합은 이를 제공하지 않습니다. [guard.py:35](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:35), [entrypoint.sh:17](/Users/allieus/Apps/django-lab/spikes/litestream/docker/entrypoint.sh:17), [management/__init__.py:415](/Users/allieus/Apps/django-lab/runs/radar-spike-sqlite-layer-20261007-161819/.venv/lib/python3.13/site-packages/django/core/management/__init__.py:415)

3. **복제 지연의 정의를 바꾸십시오.** 마지막 파일 시각·PUT 증가 여부를 곧바로 지연이나 복구 가능성으로 표시하지 말고, 쓰기 진행·원격 진행·관측 실패를 구분하십시오. [설계:84](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:84), [restore 문서](https://litestream.io/reference/restore/)

4. **벤치 결론을 정정하십시오.** “50ms 하한”, “유휴 시 읽기만 수행”, “중복 0이므로 원자적 선점 검증”은 유지할 수 없습니다. 특히 aio `total_changes` 오류를 위험 목록에 추가해야 합니다. [layers/__init__.py:319](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/__init__.py:319), [aio.py:164](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/channels-lite/src/channels_lite/layers/aio.py:164)

5. **VFS의 영속 연결 강제와 자동 배포 환경 판정을 제외하십시오.** ASGI 검증 없이 WSGI 결과를 강제 정책으로 만들지 말고, 프로세스 수·스토리지 종류는 관측 범위와 미확인 상태를 명시하십시오. [d3_d5.py:78](/Users/allieus/Apps/django-lab/spikes/litestream/docker/d3_d5.py:78), [Django async ORM](https://docs.djangoproject.com/en/6.0/topics/async/#queries-the-orm), [checks.py:502](/Users/allieus/Apps/itda-work/django-wireview/wireview/checks.py:502)

## consider (보완)

- **[판단] 회귀 시나리오를 성공 사례보다 실패 경계 중심으로 확장하십시오.** 메타만 유실, DB만 과거로 복원, 미복제 로컬 커밋, S3 조회 불가, 같은 TXID의 다른 DB, 격리 중 종료, restore 실패 후 재시작이 필요합니다. 기존 D4는 정상 정지와 대기 시간을 둔 순차 전환입니다. [d3_d5.py:45](/Users/allieus/Apps/django-lab/spikes/litestream/docker/d3_d5.py:45)

- **[판단] 기계 판독 출력과 실제 사용 설정을 활용하십시오.** 0.5.17의 `ltx`에는 JSON과 전체 레벨 옵션이 있습니다. 다만 이것만 적용해도 DB 이력의 동일성 문제가 해결되는 것은 아닙니다. [v0.5.17 ltx.go](https://raw.githubusercontent.com/benbjohnson/litestream/v0.5.17/cmd/litestream/ltx.go), [guard.py:23](/Users/allieus/Apps/django-lab/spikes/litestream/docker/guard.py:23)

- **[판단] VFS 우회는 전역 부작용까지 기록하십시오.** `sqlite3_reset_auto_extension()`은 Litestream의 등록만 골라 해제하지 않고 모든 자동 확장을 해제합니다. 현재 코드의 예외 무시까지 포함해 범용 라이브러리 기본 동작으로는 부적합합니다. [vfs.py:20](/Users/allieus/Apps/django-lab/spikes/litestream/docker/app/config/vfs.py:20), [SQLite reset_auto_extension](https://sqlite.org/c3ref/reset_auto_extension.html)

- **[판단] “아무도 없다”, “첫 사례”는 검증된 범위로 좁히십시오.** 제공된 소스와 실험으로 생태계 전체의 부재를 증명할 수는 없습니다. **[확인 필요]** 경쟁 조사와 별개로, “이번 조사 대상에는 없었다”가 근거에 맞는 표현입니다. [설계:60](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:60)

## 동의하는 점

- **설정 생성보다 운영·복구 검증에 집중하는 방향**에 동의합니다. 표준 Django 옵션과 dj-lite 결과를 그대로 수용할 수 있습니다. [configurator.py:106](/Users/allieus/Apps/django-lab/radar/spikes/sqlite-pkg/ext/dj-lite/src/dj_lite/configurator.py:106)
- **wireview transport와 채널 레이어를 별도 책임으로 유지하는 경계**에 동의합니다. 기존 인터페이스가 이를 뒷받침합니다. [transport.py:57](/Users/allieus/Apps/itda-work/django-wireview/wireview/core/transport.py:57)
- **단일 쓰기 노드·정지 후 기동·버전별 장애 회귀 검증**에 동의합니다. 단, 부팅 가드가 이 배포 계약을 대신 보장한다고 표현해서는 안 됩니다. [Docker 보고서:61](/Users/allieus/hermes-outbox/2026-10-07/litestream-django-docker.md:61), [설계:85](/Users/allieus/hermes-outbox/2026-10-07/sqlite-django-package-review-v0.md:85)