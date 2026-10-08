"""볼륨 ``/data`` 의 상태를 JSON 한 줄로 출력한다. 볼륨은 바꾸지 않는다.

DB 를 직접 열면 ``-wal``·``-shm`` 이 생기거나 바뀌므로, DB 와 사이드카를 ``/tmp`` 로 복사한 뒤
복사본을 연다. 격리 디렉터리(``*.stale-*``) 안의 DB 도 같은 방법으로 센다.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

DATA = Path(sys.argv[1] if len(sys.argv) > 1 else "/data")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_rows(db: Path) -> int | str:
    tmp = Path(tempfile.mkdtemp(prefix="inspect-"))
    try:
        copy = tmp / db.name
        shutil.copy2(db, copy)
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.is_file():
                shutil.copy2(side, tmp / side.name)
        con = sqlite3.connect(copy)
        try:
            return con.execute("SELECT count(*) FROM notes_note").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return f"error: {exc}"
    finally:
        shutil.rmtree(tmp)


def entry(path: Path) -> dict:
    st = path.lstat()
    info = {"uid": st.st_uid, "gid": st.st_gid, "mode": oct(st.st_mode & 0o7777)}
    if path.is_symlink():
        info["type"] = "link"
    elif path.is_dir():
        info["type"] = "dir"
        info["children"] = sorted(p.name for p in path.iterdir())
    else:
        info["type"] = "file"
        info["size"] = st.st_size
        if not path.name.endswith(".lock"):
            info["sha256"] = sha256(path)
        if path.name.endswith(".sqlite3"):
            info["rows"] = count_rows(path)
    return info


def main() -> None:
    st = DATA.stat()
    out = {"data": {"uid": st.st_uid, "gid": st.st_gid}, "entries": {}}
    for root, dirs, files in os.walk(DATA):
        dirs.sort()
        for name in sorted(dirs + files):
            p = Path(root) / name
            out["entries"][str(p.relative_to(DATA))] = entry(p)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
