#!/usr/bin/env bash
# GitHub Actions CI(.github/workflows/ci.yml)를 act 로 로컬에서 돌린다.
#
#   scripts/ci-local.sh                      # lint + 전체 매트릭스
#   scripts/ci-local.sh lint                 # lint 만
#   scripts/ci-local.sh test 3.14 6.1        # 매트릭스 한 칸 (python, django)
#   CI_ARCH=amd64 scripts/ci-local.sh        # GitHub 러너와 같은 amd64 (에뮬레이션이라 느리다)
#
# 기본 이미지와 아키텍처는 저장소 루트의 .actrc 에 있다.
set -euo pipefail

cd "$(dirname "$0")/.."

command -v act >/dev/null || { echo "act 가 없다: brew install act" >&2; exit 127; }
docker info >/dev/null 2>&1 || { echo "Docker 데몬이 응답하지 않는다" >&2; exit 1; }

# act 는 docker context 를 읽지 않고 /var/run/docker.sock 만 본다.
# colima·OrbStack 처럼 소켓이 다른 곳에 있으면 현재 context 의 엔드포인트를 넘긴다.
if [[ -z "${DOCKER_HOST:-}" ]]; then
  DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
  export DOCKER_HOST
fi

# 잡 안에서 Docker 를 쓰지 않으므로 소켓을 컨테이너에 마운트하지 않는다.
# (회귀 랩 잡이 생기면 다시 본다. DESIGN §10)
args=(push --container-daemon-socket -)
if [[ -n "${CI_ARCH:-}" ]]; then
  args+=(--container-architecture "linux/${CI_ARCH}")
fi

case "${1:-all}" in
  all) ;;
  lint) args+=(-j lint) ;;
  test)
    args+=(-j test)
    [[ -n "${2:-}" ]] && args+=(--matrix "python:$2")
    [[ -n "${3:-}" ]] && args+=(--matrix "django:$3")
    ;;
  *) echo "사용법: $0 [all|lint|test [python] [django]]" >&2; exit 64 ;;
esac

exec act "${args[@]}"
