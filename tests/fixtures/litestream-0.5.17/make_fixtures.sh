#!/usr/bin/env bash
# litestream 0.5.17 실측 fixture 를 다시 만든다. 사용법은 같은 폴더의 README.md.
#   make_fixtures.sh <새 랩 디렉터리(없어야 함)>
# 랩 디렉터리의 절대 경로는 fixture 에서 '$LAB' 으로 바꿔 저장한다.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 NEW_LAB_DIR" >&2
  exit 64
fi
if [ -e "$1" ]; then
  echo "refusing: $1 already exists (use a new directory)" >&2
  exit 64
fi
mkdir -p "$1"
LAB=$(cd "$1" && pwd -P)
OUT=$(cd "$(dirname "$0")" && pwd -P)
LS=${LITESTREAM:-litestream}

# 한 명령의 stdout·stderr·rc 를 OUT/<name>/ 에 저장한다. 랩 경로는 $LAB 로 치환한다.
capture() {
  local name=$1
  shift
  mkdir -p "$OUT/$name"
  local rc=0
  "$@" >"$LAB/.stdout" 2>"$LAB/.stderr" || rc=$?
  sed "s#$LAB#\$LAB#g" "$LAB/.stdout" >"$OUT/$name/stdout"
  sed "s#$LAB#\$LAB#g" "$LAB/.stderr" >"$OUT/$name/stderr"
  echo "$rc" >"$OUT/$name/rc"
}

cd "$LAB"
cat >ls.yml <<EOF
l0-retention: 5s
l0-retention-check-interval: 2s
levels:
  - interval: 5s
  - interval: 10s
snapshot:
  interval: 20s
  retention: 1h
dbs:
  - path: $LAB/app.db
    checkpoint-interval: 5s
    replica:
      type: file
      path: $LAB/replica
  - path: $LAB/empty.db
    replica:
      type: file
      path: $LAB/replica-empty
EOF

capture version "$LS" version

# 복제본 디렉터리 없음 / 빈 디렉터리 / 설정에 없는 DB / 설정 파일 없음 / 깨진 YAML
capture ltx_replica_missing "$LS" ltx -config ls.yml -level all -json "$LAB/app.db"
mkdir replica-empty
capture ltx_replica_empty_dir "$LS" ltx -config ls.yml -level all -json "$LAB/empty.db"
capture ltx_db_not_in_config "$LS" ltx -config ls.yml -level all -json "$LAB/other.db"
capture ltx_config_missing "$LS" ltx -config "$LAB/nope.yml" -level all -json "$LAB/app.db"
printf 'dbs: [\n' >bad.yml
capture ltx_bad_yaml "$LS" ltx -config bad.yml -level all -json "$LAB/app.db"
capture restore_no_backups "$LS" restore -config ls.yml -json -o "$LAB/out-empty.db" "$LAB/empty.db"

# databases: 설정의 DB 목록. 경로 해석(링크·'..'·상대 경로·dir:)을 본다. 상대 경로와 dir: 항목은
# Litestream 의 작업 디렉터리 기준으로 풀리므로 하위 디렉터리에서 돌린다($LAB/cwd 로 저장된다).
mkdir real cwd dirdb
ln -s real link
cat >dbs.yml <<EOF
dbs:
  - path: $LAB/app.db
    replica:
      type: file
      path: $LAB/replica
  - path: $LAB/link/linked.db
    replica:
      type: file
      path: $LAB/replica-linked
  - path: $LAB/real/../dotdot.db
    replica:
      type: file
      path: $LAB/replica-dotdot
  - path: relative.db
    replica:
      type: file
      path: $LAB/replica-relative
  - dir: $LAB/dirdb
    pattern: "*.db"
    replica:
      type: file
      path: $LAB/replica-dir
EOF
(cd cwd && capture databases_json "$LS" databases -config "$LAB/dbs.yml" -json)
capture databases_config_missing "$LS" databases -config "$LAB/nope.yml" -json
capture databases_bad_yaml "$LS" databases -config bad.yml -json
printf 'dbs: []\n' >empty.yml
capture databases_empty "$LS" databases -config empty.yml -json

# 실제 복제: 쓰기 → 업로드 → 압축·L0 보존 정리 → 종료
sqlite3 app.db "PRAGMA journal_mode=WAL; CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);" >/dev/null
"$LS" replicate -config ls.yml >replicate.log 2>&1 &
pid=$!
for _ in $(seq 1 40); do
  sqlite3 app.db "INSERT INTO t(v) VALUES(hex(randomblob(3000)));" || true
  sleep 0.5
done
sleep 45
kill "$pid"
wait "$pid" || true

capture ltx_all_levels "$LS" ltx -config ls.yml -level all -json "$LAB/app.db"
capture ltx_l0_default "$LS" ltx -config ls.yml -json "$LAB/app.db"
capture ltx_all_levels_text "$LS" ltx -config ls.yml -level all "$LAB/app.db"
# 로컬 메타 디렉터리(.app.db-litestream) 를 그대로 복사한다. 업로드·정리·종료 뒤의 상태다.
META_OUT="$OUT/local_meta_after_replicate"
rm -rf -- "$META_OUT"
mkdir -p "$META_OUT"
cp -R "$LAB/.app.db-litestream" "$META_OUT/meta"
# 낮은 TXID 의 실제 LTX 파일 하나(복제본 L1 의 첫 파일). 로컬 메타 검증 테스트가 쓴다.
SAMPLE_OUT="$OUT/ltx_sample"
rm -rf -- "$SAMPLE_OUT"
mkdir -p "$SAMPLE_OUT"
first_l1=$(cd "$LAB/replica/ltx/1" && ls | sort | head -n 1)
cp "$LAB/replica/ltx/1/$first_l1" "$SAMPLE_OUT/$first_l1"

# L0 만 사라진 복제본(외부 lifecycle·부분 복사 흉내). 기본(L0)은 빈 목록, -level all 은 최대값을 본다.
cp -R replica replica-nol0
NOL0="$LAB/replica-nol0/ltx/0"
rm -r -- "$NOL0"
capture ltx_no_l0_default "$LS" ltx -json "file://$LAB/replica-nol0"
capture ltx_no_l0_all_levels "$LS" ltx -level all -json "file://$LAB/replica-nol0"

# 접근 불가 복제본
cp -R replica replica-denied
chmod 000 replica-denied
cat >denied.yml <<EOF
dbs:
  - path: $LAB/app.db
    replica:
      type: file
      path: $LAB/replica-denied
EOF
capture ltx_permission_denied "$LS" ltx -config denied.yml -level all -json "$LAB/app.db"
chmod 755 replica-denied

# restore: 새 경로 성공 / 이미 있는 경로 거부
capture restore_ok_json "$LS" restore -config ls.yml -json -integrity-check quick -o "$LAB/restored.db" "$LAB/app.db"
capture restore_output_exists "$LS" restore -config ls.yml -json -o "$LAB/restored.db" "$LAB/app.db"

echo "fixtures written to $OUT (lab: $LAB)"
