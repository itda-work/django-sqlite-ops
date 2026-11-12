# Litestream 0.5.18 실측 fixture

`boot/litestream.py` 의 파싱 테스트(와 `sqlite_doctor` 의 Litestream 설정 대조 테스트)가 쓰는 실제 출력이다. 각 폴더에 `stdout`·`stderr`·`rc`
가 있다. 랩 디렉터리의 절대 경로는 `$LAB` 으로 바꿔 저장했다.

## 만든 법

2026-11-12(#99), macOS arm64, 릴리스 `litestream-0.5.18-darwin-arm64` 바이너리(같은 릴리스의
`checksums.txt` 와 SHA-256 일치). brew 의 0.5.17 은 그대로 두고 `LITESTREAM` 으로 바꿔 돌렸다.

```sh
LITESTREAM=/path/to/litestream-0.5.18/bin/litestream \
  tests/fixtures/make_litestream_fixtures.sh 0.5.18 .work/issue-99/lab/run-1
```

스크립트는 바이너리의 `version` 이 첫 인자(판)와 다르거나 대상 폴더(`tests/fixtures/litestream-<판>/`)가
이미 있으면 거부한다. 둘째 인자는 아직 없는 새 디렉터리다. `sqlite3` CLI 가 필요하고 약 70초 걸린다
(쓰기 20초 + 압축·L0 보존 정리 대기 45초). 판별 기대값은 `tests/_litestream.py` 의 `FIXTURE_EXPECTED`
(이 판의 최대 TXID `0x19`)다.

복제 설정(스크립트가 만든다): `file://` 복제본, `l0-retention: 5s`, 레벨 L1 5s·L2 10s,
스냅샷 20s, `checkpoint-interval: 5s`. 기본값(L0 보존 5분 등)으로는 정리가 늦어 관찰이 오래 걸린다.

## 목록

| 폴더 | 명령(모두 `litestream ...`) | rc | 요점 |
|---|---|---|---|
| `version` | `version` | 0 | stdout `0.5.18` |
| `ltx_replica_missing` | `ltx -config ls.yml -level all -json $LAB/app.db` (복제본 디렉터리 없음) | 0 | `[]` |
| `ltx_replica_empty_dir` | 같은 명령, 복제본 디렉터리는 있으나 비어 있음 | 0 | `[]` — 위와 구분되지 않는다 |
| `ltx_db_not_in_config` | `ltx -config ls.yml ... $LAB/other.db` | 1 | `Error: database not found in config: ...` |
| `ltx_config_missing` | `ltx -config $LAB/nope.yml ...` | 1 | `Error: config file not found: ...` |
| `ltx_bad_yaml` | `ltx -config bad.yml ...` (`dbs: [`) | 1 | `Error: yaml: ...` |
| `ltx_permission_denied` | 복제본 디렉터리 `chmod 000` | 1 | `Error: open .../ltx/0: permission denied` |
| `ltx_all_levels` | replicate 로 쓰기 40회·정리 대기 뒤 `ltx -level all -json` | 0 | L0·L1·L2·L9, 최대 `0x19`(15항목. 0.5.17 은 16항목 — L2·L9 의 압축 묶음이 실행 시각에 따라 다르다) |
| `ltx_all_levels_text` | 같은 복제본, `-json` 없이 | 0 | 사람용 표(파싱하지 않는다. 참고용) |
| `ltx_l0_default` | 같은 복제본, `-level` 생략 | 0 | L0 만. 정상 정리는 최신 L0 를 남긴다 |
| `local_meta_after_replicate/meta` | `cp -R .app.db-litestream` (replicate 종료 뒤) | – | 실제 메타 사본. `ltx/0/<max>-<max>.ltx` 하나가 남는다 |
| `ltx_sample` | 복제본 `ltx/1` 의 이름순 첫 파일 복사 | – | 낮은 TXID 의 실제 LTX(로컬 메타 검증 테스트용) |
| `ltx_no_l0_default` | 복제본 사본에서 `ltx/0` 만 지운 뒤 `ltx -json file://...` | 0 | `[]` — L0 만 보면 빈 복제본으로 오판 |
| `ltx_no_l0_all_levels` | 같은 사본, `-level all` | 0 | 최대 `0x19` |
| `restore_ok_json` | `restore -config ls.yml -json -integrity-check quick -o $LAB/restored.db $LAB/app.db` | 0 | stdout 에 **로그 한 줄 + JSON** 이 섞인다 |
| `restore_output_exists` | 같은 명령을 한 번 더 | 1 | `Error: cannot restore, output path already exists and is not empty` |
| `databases_json` | `databases -config dbs.yml -json`, 작업 디렉터리 `$LAB/cwd` | 0 | 링크 경로는 그대로, `real/../dotdot.db` 는 `dotdot.db` 로 접힘, 상대 경로 `relative.db` 는 `$LAB/cwd/relative.db`, `dir:` 항목은 디렉터리가 아니라 **작업 디렉터리** `$LAB/cwd` 로 나온다 |
| `databases_config_missing` | `databases -config $LAB/nope.yml -json` | 1 | `Error: config file not found: ...` |
| `databases_bad_yaml` | `databases -config bad.yml -json` | 1 | `Error: yaml: ...` |
| `databases_empty` | `databases -config empty.yml -json` (`dbs: []`) | 0 | `[]` |
| `restore_no_backups` | 빈 복제본에서 restore | 1 | `Error: no matching backup files available` |

## 0.5.17 과의 차이 (2026-11-12, #99)

모든 폴더의 `rc`·`stderr` 가 0.5.17 과 바이트 단위로 같다. `stdout` 이 다른 폴더는 아래뿐이고 모두 값 차이다.

| 폴더 | 차이 | 구분 |
|---|---|---|
| `version` | `0.5.17` → `0.5.18` | 판 |
| `ltx_all_levels`·`ltx_no_l0_all_levels` | `timestamp`·`size`, 그리고 L2·L9 의 압축 묶음(0.5.18 은 L2 `0b-16` 하나, 0.5.17 은 `0b-10`·`11-16` 둘). 항목의 키 집합·최대 TXID `0x19` 는 같다 | 값(실행 시각에 따른 압축 경계) |
| `ltx_all_levels_text` | 위와 같은 값 차이(사람용 표, 파싱하지 않는다) | 값 |
| `ltx_l0_default` | `timestamp` | 값 |
| `restore_ok_json` | 로그 줄의 `time=`, JSON 의 `duration_ms`. 로그 줄 형식(`time=… level=INFO msg="post-restore integrity check passed" db=app.db replica=file`)과 JSON 키 집합(`db_path`·`duration_ms`·`integrity_check`·`replica`·`txid`)은 같다 | 값 |
| `local_meta_after_replicate`·`ltx_sample` | LTX 헤더의 시각·salt·체크섬. 이름(`19-19`, `01-01`)과 헤더 배치(flags·page size·min/max TXID·WAL offset/size)는 같다 | 값 |
