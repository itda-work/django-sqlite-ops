"""부팅 가드(스파이크): 로컬 DB 가 복제본보다 뒤처졌으면 로컬을 치우고 복원하게 만든다.

근거: litestream 은 메타 디렉터리(.<db>-litestream/)에 로컬이 마지막으로 복제한 TXID 를 남긴다.
복제본의 최대 TXID(`litestream ltx` 의 max_txid 최대값)가 로컬 메타의 TXID 보다 크면,
다른 머신이 그 뒤로 더 썼다는 뜻이다 → 로컬 DB 를 격리(rename)하고 restore 하게 한다.
로컬 메타를 읽을 수 없으면 보수적으로 '복제본이 있으면 복원'을 택한다.
"""
import os, subprocess, sys, pathlib, time

db = pathlib.Path(os.environ["DB_PATH"])
url = os.environ["REPLICA_URL"]

def log(*a):
    print("[guard]", *a, file=sys.stderr, flush=True)

if not db.exists():
    log("no local db → normal restore path"); sys.exit(0)

r = subprocess.run(["litestream", "ltx", url], capture_output=True, text=True)
rows = [l.split() for l in r.stdout.strip().splitlines()[1:] if l.strip()]
if r.returncode or not rows:
    log("replica empty/unreachable → keep local", r.stderr.strip()[-200:]); sys.exit(0)
remote = max(int(x[2], 16) for x in rows)

meta = db.parent / f".{db.name}-litestream"
local = None
# 로컬 메타의 ltx 파일 이름(<min>-<max>.ltx) 중 최대 TXID
for p in meta.rglob("*.ltx"):
    try:
        local = max(local or 0, int(p.stem.split("-")[1], 16))
    except Exception:
        pass
log(f"local_txid={local} remote_txid={remote}")
if local is None or remote > local:
    q = db.with_name(db.name + f".stale-{int(time.time())}")
    for f in db.parent.glob(db.name + "*"):
        if ".stale-" in f.name:
            continue
        f.rename(f.with_name(f.name.replace(db.name, q.name, 1)))
    if meta.exists():
        meta.rename(meta.with_name(meta.name + f".stale-{int(time.time())}"))
    log(f"local is behind → quarantined as {q.name}, will restore from replica")
else:
    log("local is up to date → keep")
