#!/bin/bash
# Django + Litestream 컨테이너 엔트리포인트 (스파이크)
# GUARD=1 이면 부팅 전에 guard.py 로 '오래된 로컬 DB' 를 검사한다(함정 B 방지).
set -euo pipefail
mkdir -p /data
cat > /etc/litestream.yml <<EOF
dbs:
  - path: ${DB_PATH}
    replica:
      url: ${REPLICA_URL}
      sync-interval: ${SYNC_INTERVAL:-1s}
EOF

if [ "${GUARD:-0}" = "1" ]; then
  python /usr/local/bin/guard.py
fi
litestream restore -if-db-not-exists -if-replica-exists "${DB_PATH}"
python manage.py migrate --noinput -v0
exec litestream replicate -exec "gunicorn config.wsgi -w ${WORKERS:-4} -b 0.0.0.0:8000 --graceful-timeout 10 --log-level warning"
