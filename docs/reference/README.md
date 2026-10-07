# 스파이크 코드 (참고용, 그대로 쓰지 말 것)

2026-10-07 Litestream × Django Docker 랩(`~/Apps/django-lab/spikes/litestream/docker/`)에서 가져왔다.

- `guard_spike.py`: 부팅 가드 스파이크. **결함이 있다**(DESIGN §4-4). 원격 조회 실패를 통과로 처리하고, 메타가 없으면 로컬을 버리며, 격리가 원자적이지 않고, L0 만 본다.
- `vfs_workaround_spike.py`: Linux VFS 확장 로드 우회(`sqlite3_reset_auto_extension`). 모든 자동 확장을 해제한다.
- `Dockerfile`, `entrypoint.sh`, `compose.yaml`: 랩 구성. WSGI(gunicorn)였다. 자격증명은 로컬 더미 값(`any`)이다.
