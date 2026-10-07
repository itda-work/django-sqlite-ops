# Litestream 0.5.17 실측 fixture

`boot/litestream.py` 의 파싱 테스트가 쓰는 실제 출력이다. 각 폴더에 `stdout`·`stderr`·`rc`
가 있다. 랩 디렉터리의 절대 경로는 `$LAB` 으로 바꿔 저장했다.

## 다시 만드는 법

```sh
# macOS: brew install benbjohnson/litestream/litestream  (https://litestream.io/install/mac/)
litestream version   # 0.5.17 이어야 한다
tests/fixtures/litestream-0.5.17/make_fixtures.sh .work/issue-3/lab/run-fixtures-<번호>
```

인자는 아직 없는 새 디렉터리다(있으면 거부한다). `sqlite3` CLI 가 필요하다. 약 70초 걸린다
(쓰기 20초 + 압축·L0 보존 정리 대기 45초). `LITESTREAM=/path/to/litestream` 으로 바이너리를
바꿀 수 있다. 다시 만들면 TXID·크기·시각이 바뀔 수 있으므로 `tests/test_boot_litestream.py` 의
기대값(현재 최대 TXID `0x19`)도 맞춘다.

복제 설정(스크립트가 만든다): `file://` 복제본, `l0-retention: 5s`, 레벨 L1 5s·L2 10s,
스냅샷 20s, `checkpoint-interval: 5s`. 기본값(L0 보존 5분 등)으로는 정리가 늦어 관찰이 오래 걸린다.

## 목록

| 폴더 | 명령(모두 `litestream ...`) | rc | 요점 |
|---|---|---|---|
| `version` | `version` | 0 | stdout `0.5.17` |
| `ltx_replica_missing` | `ltx -config ls.yml -level all -json $LAB/app.db` (복제본 디렉터리 없음) | 0 | `[]` |
| `ltx_replica_empty_dir` | 같은 명령, 복제본 디렉터리는 있으나 비어 있음 | 0 | `[]` — 위와 구분되지 않는다 |
| `ltx_db_not_in_config` | `ltx -config ls.yml ... $LAB/other.db` | 1 | `Error: database not found in config: ...` |
| `ltx_config_missing` | `ltx -config $LAB/nope.yml ...` | 1 | `Error: config file not found: ...` |
| `ltx_bad_yaml` | `ltx -config bad.yml ...` (`dbs: [`) | 1 | `Error: yaml: ...` |
| `ltx_permission_denied` | 복제본 디렉터리 `chmod 000` | 1 | `Error: open .../ltx/0: permission denied` |
| `ltx_all_levels` | replicate 로 쓰기 40회·정리 대기 뒤 `ltx -level all -json` | 0 | L0·L1·L2·L9, 최대 `0x19` |
| `ltx_all_levels_text` | 같은 복제본, `-json` 없이 | 0 | 사람용 표(파싱하지 않는다. 참고용) |
| `ltx_l0_default` | 같은 복제본, `-level` 생략 | 0 | L0 만. 정상 정리는 최신 L0 를 남긴다 |
| `local_meta_after_replicate` | `find .app.db-litestream -type f` (replicate 종료 뒤) | – | `ltx/0/<max>-<max>.ltx` 하나가 남는다 |
| `ltx_no_l0_default` | 복제본 사본에서 `ltx/0` 만 지운 뒤 `ltx -json file://...` | 0 | `[]` — L0 만 보면 빈 복제본으로 오판 |
| `ltx_no_l0_all_levels` | 같은 사본, `-level all` | 0 | 최대 `0x19` |
| `restore_ok_json` | `restore -config ls.yml -json -integrity-check quick -o $LAB/restored.db $LAB/app.db` | 0 | stdout 에 **로그 한 줄 + JSON** 이 섞인다 |
| `restore_output_exists` | 같은 명령을 한 번 더 | 1 | `Error: cannot restore, output path already exists and is not empty` |
| `restore_no_backups` | 빈 복제본에서 restore | 1 | `Error: no matching backup files available` |
