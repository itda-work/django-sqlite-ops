#!/usr/bin/env bash
# 회귀 랩(#10, DESIGN §10)을 Docker 로 돌린다. 자세한 내용은 lab/README.md.
#
#   scripts/lab.sh up                 # 이미지 빌드 + SeaweedFS·toxiproxy 기동(남겨 둔다)
#   scripts/lab.sh run [L1..L8|P1|P2|all]   # 시나리오(끝나면 down -v)
#   scripts/lab.sh bench [pytest 인자]   # PRAGMA 벤치(끝나면 down -v)
#   scripts/lab.sh soak [pytest 인자]    # fd·RSS 장시간 실행(#33, 끝나면 down -v)
#   scripts/lab.sh down               # 이 랩의 컨테이너·볼륨·네트워크만 지운다
#
# 프로젝트 이름은 LAB_PROJECT(기본 dso-lab, 또는 dso-lab-<suffix>)다. 같은 호스트의 다른
# 프로젝트를 건드리지 않도록 정리는 `docker compose -p <우리 프로젝트> down -v --remove-orphans`
# 만 쓴다. prune·전체 대상 rm 은 쓰지 않는다.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${LAB_PROJECT:-dso-lab}"
case "$PROJECT" in
  dso-lab | dso-lab-[a-z0-9]*) ;;
  *) echo "LAB_PROJECT must be dso-lab or dso-lab-<suffix>: $PROJECT" >&2; exit 64 ;;
esac
export LAB_PROJECT="$PROJECT"
export LAB_TAG="${LAB_TAG:-$(git rev-parse --short HEAD)}"
BUILD=lab/.build
OUT="${LAB_OUT:-lab/.out}"

command -v docker >/dev/null || { echo "docker 가 없다" >&2; exit 127; }
docker info >/dev/null 2>&1 || { echo "Docker 데몬이 응답하지 않는다" >&2; exit 1; }

# 스택: 시나리오용(기반 compose), P1(single-server 문서 compose), P2(multiproc 문서 compose)
stack_files() {
  case "$1" in
    main) echo "-f $BUILD/lab-compose.yaml" ;;
    p1) echo "-f $BUILD/lab-compose.yaml -f $BUILD/compose.yaml -f $BUILD/profile-override.yaml" ;;
    p2) echo "-f $BUILD/lab-compose.yaml -f $BUILD/compose-multiproc.yaml -f $BUILD/profile-override.yaml -f $BUILD/multiproc-override.yaml" ;;
  esac
}
stack_project() {
  case "$1" in
    main) echo "$PROJECT" ;;
    *) echo "$PROJECT-$1" ;;
  esac
}

build() {
  mkdir -p "$OUT"
  local dist
  dist="$(mktemp -d "${TMPDIR:-/tmp}/dso-lab-dist.XXXXXX")"
  uv build --wheel --out-dir "$dist" >/dev/null
  local wheel
  wheel="$(ls "$dist"/*.whl)"
  uv run --no-project python lab/build_context.py "$wheel" >/dev/null
  # uv build 가 .gitignore 도 남긴다. mktemp 로 만든 디렉터리 하나만 지운다.
  rm -rf -- "${dist:?}"
  docker build --label io.itda.dso-lab=1 -t "dso-lab-app:$LAB_TAG" "$BUILD"
}

down() {
  [[ -f "$BUILD/lab-compose.yaml" ]] || return 0
  local s
  for s in main p1 p2; do
    # shellcheck disable=SC2046 # 파일 인자는 단어로 나뉘어야 한다
    docker compose -p "$(stack_project "$s")" --project-directory "$BUILD" $(stack_files "$s") \
      --profile app down -v --remove-orphans --timeout 30 || true
  done
  echo "--- 남은 랩 자원 (비어 있어야 한다)"
  docker ps -a --filter label=io.itda.dso-lab=1
  docker volume ls --filter label=io.itda.dso-lab=1
  docker network ls --filter label=io.itda.dso-lab=1
}

pytest_lab() {
  RUN_LAB=1 uv run --group dev pytest lab -v -rA "$@"
}

cmd="${1:-}"
shift || true
case "$cmd" in
  up)
    build
    python3 lab/_lab.py up main
    ;;
  run)
    trap down EXIT
    build
    stamp="$(date +%Y%m%d-%H%M%S)"
    sel="${1:-all}"
    if [[ "$sel" == all ]]; then
      pytest_lab -m "not bench and not soak" 2>&1 | tee "$OUT/run-$stamp.log"
    else
      pytest_lab -m "not bench and not soak" -k "$sel" 2>&1 | tee "$OUT/run-$stamp.log"
    fi
    ;;
  bench)
    trap down EXIT
    build
    stamp="$(date +%Y%m%d-%H%M%S)"
    pytest_lab -m bench "$@" 2>&1 | tee "$OUT/bench-$stamp.log"
    ;;
  soak)
    trap down EXIT
    build
    stamp="$(date +%Y%m%d-%H%M%S)"
    pytest_lab -m soak "$@" 2>&1 | tee "$OUT/soak-$stamp.log"
    ;;
  down)
    down
    ;;
  *)
    echo "사용법: $0 up | run [L1..L8|P1|P2|all] | bench | soak | down" >&2
    exit 64
    ;;
esac
