"""복제본 상태를 JSON 한 줄로 출력한다: 최대 TXID 와 복원본의 행 수.

``litestream restore`` 로 ``/tmp`` 에 복원해 센다. 볼륨을 붙이지 않은 컨테이너에서 돈다.
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from django_sqlite_ops.boot import litestream as ls
from django_sqlite_ops.boot.decide import RemoteTxid

CONFIG = "/etc/litestream.yml"
DB = "/data/app.sqlite3"


def main() -> None:
    remote = ls.remote_max_txid(Path(DB), config=Path(CONFIG), timeout=30)
    out: dict = {"remote": type(remote).__name__}
    if type(remote) is RemoteTxid:
        out["txid"] = remote.txid
    else:
        out["detail"] = getattr(remote, "message", None)
        print(json.dumps(out))
        return
    tmp = Path(tempfile.mkdtemp(prefix="replica-"))
    target = tmp / "r.sqlite3"
    proc = subprocess.run(
        ["litestream", "restore", "-config", CONFIG, "-o", str(target), DB],
        capture_output=True,
        text=True,
        timeout=120,
    )
    out["restore_rc"] = proc.returncode
    if proc.returncode != 0:
        out["restore_stderr"] = proc.stderr.strip().splitlines()[-3:]
    else:
        con = sqlite3.connect(target)
        try:
            out["rows"] = con.execute("SELECT count(*) FROM notes_note").fetchone()[0]
            out["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
    print(json.dumps(out))


if __name__ == "__main__":
    sys.exit(main())
