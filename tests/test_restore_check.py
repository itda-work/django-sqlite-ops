"""``sqlite_doctor --restore-test`` (DESIGN §6-2 ``--restore-test``, #54).

명령은 ``test_doctor.py`` 처럼 새 인터프리터에서 ``settings.configure()`` 뒤에 실행한다.
실제 Litestream 이 필요한 시나리오는 바이너리가 없으면 skip 한다(``litestream`` CI 잡에서는
``REQUIRE_LITESTREAM=1`` 이라 실패한다). 인자·경로 거부·정리 규칙·타임아웃·무결성은 가짜
litestream(파이썬 스크립트)으로 모든 잡에서 돈다.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _litestream import LITESTREAM, SilentServer, needs_litestream, write_config

from django_sqlite_ops import restore_check as rc
from django_sqlite_ops.boot import litestream as ls

ROOT = Path(__file__).resolve().parent.parent

RUN_SCRIPT = """
import json
import sys

import django
from django.conf import settings

config = json.loads(sys.argv[1])
settings.configure(INSTALLED_APPS=["django_sqlite_ops"], USE_TZ=True, **config)
django.setup()
exec(sys.argv[2])

# Django 가 DB 연결을 만들면 별칭을 적는다(doctor 는 진단 뒤 연결을 닫으므로 호출을 센다).
from django.db.backends.sqlite3.base import DatabaseWrapper

opened = []
_new = DatabaseWrapper.get_new_connection


def _record(self, params):
    opened.append(self.alias)
    return _new(self, params)


DatabaseWrapper.get_new_connection = _record

from django.core.management import execute_from_command_line

try:
    execute_from_command_line(["manage.py", "sqlite_doctor", *sys.argv[3:]])
finally:
    print("OPENED=" + json.dumps(opened), file=sys.stderr)
"""


def run(databases, *args, post="", json_output=True, **extra):
    """명령을 실행해 ``(exit code, JSON 또는 stdout, stderr)``. Django 연결이 열리면 실패."""
    config = {"DATABASES": databases, **extra}
    argv = [sys.executable, "-c", RUN_SCRIPT, json.dumps(config), post, *args]
    if json_output:
        argv.append("--json")
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode in (0, 1, 2), result.stderr
    assert "OPENED=[]" in result.stderr, result.stderr
    if not json_output or result.returncode == 2 and not result.stdout:
        return result.returncode, result.stdout, result.stderr
    data = json.loads(result.stdout)
    assert data["exit_code"] == result.returncode
    assert {i["section"] for i in data["items"]} <= {"restore"}
    return result.returncode, data, result.stderr


def items(data, key=None, alias="default"):
    return [i for i in data["items"] if (key is None or i["key"] == key) and i["alias"] == alias]


def one(data, key, alias="default"):
    found = items(data, key, alias)
    assert len(found) == 1, data["items"]
    return found[0]


def keys(data, alias="default"):
    return [i["key"] for i in items(data, alias=alias)]


def sqlite_db(path: Path, **options) -> dict:
    config = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}
    if options:
        config["OPTIONS"] = options
    return config


# SQLite 가 아닌 별칭. 드라이버 없이 불러올 수 있는 백엔드를 쓴다: 불러오지 못하는 백엔드(예:
# psycopg 없는 postgresql)가 있으면 이 모드는 거부한다(R4-1).
NOT_SQLITE = {"ENGINE": "django.db.backends.dummy", "NAME": "x"}


def make_db(path: Path, rows: int = 10, *, wal: bool = True) -> Path:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("CREATE INDEX t_v ON t(v)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", ((f"row-{i}" * 20,) for i in range(rows)))
    conn.commit()
    conn.close()
    return path


def make_big_db(path: Path, megabytes: int = 40) -> Path:
    """full 검사가 수십~수백 ms 걸리는 크기의 DB."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, k TEXT, v BLOB)")
    i = 0
    while os.path.getsize(path) < megabytes * 2**20:
        conn.executemany(
            "INSERT INTO t(k, v) VALUES (?, randomblob(400))",
            ((f"key-{i + j:012d}",) for j in range(20000)),
        )
        conn.commit()
        i += 20000
    conn.execute("CREATE INDEX t_k ON t(k)")
    conn.commit()
    conn.close()
    return path


def make_corrupt_db(path: Path) -> Path:
    """인덱스 정의를 다른 열로 바꿔 ``integrity_check`` 가 오류 줄을 내는 DB."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(x, y)")
    conn.execute("CREATE INDEX i ON t(x)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", ((n, -n) for n in range(50)))
    conn.commit()
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("UPDATE sqlite_master SET sql = 'CREATE INDEX i ON t(y)' WHERE name = 'i'")
    conn.commit()
    conn.close()
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
    return path


def fingerprint(directory: Path) -> dict:
    """디렉터리 아래 모든 항목(디렉터리 자신 포함)의 크기·mtime·inode·내용 해시."""
    out = {}
    for path in [directory, *sorted(directory.rglob("*"))]:
        st = os.lstat(path)
        digest = None
        if stat.S_ISREG(st.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[str(path.relative_to(directory))] = (st.st_size, st.st_mtime_ns, st.st_ino, digest)
    return out


# --- 가짜 litestream ------------------------------------------------------------------------

FAKE = """#!{python}
import json, os, shutil, sys, time
spec = json.load(open({spec!r}))
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
cmd = args[0]
if cmd == "version":
    print("0.5.17")
    sys.exit(0)
if cmd == "databases":
    # 0.5.17 은 dir: 항목을 감시 디렉터리가 아니라 실행한 cwd 로 내보낸다(재현함).
    # 상대 path: 는 실행한 cwd 아래로 풀린다(재현함).
    paths = spec["dbs"] + ([os.getcwd()] if spec.get("dir_entry") else [])
    paths += [os.path.join(os.getcwd(), r) for r in spec.get("relative", [])]
    print(json.dumps([{{"path": p, "replica": "file"}} for p in paths]))
    sys.exit(0)
part = spec[cmd]
time.sleep(part.get("sleep", 0))
if part.get("rc", 0):
    print(part.get("stderr", "boom"), file=sys.stderr)
    sys.exit(part["rc"])
if cmd == "ltx":
    txid = part.get("txid")
    rows = []
    if txid is not None:
        rows = [{{"level": 0, "min_txid": "%016x" % 1, "max_txid": "%016x" % txid}}]
    print(json.dumps(rows))
elif cmd == "restore":
    out = args[args.index("-o") + 1]
    shutil.copyfile(part["src"], out)
    swap = part.get("swap")
    if swap and not os.path.islink(swap):
        # 검사를 통과한 --restore-dir 을 복원 중에 링크로 바꿔치기한다(R5-4).
        os.rename(swap, swap + ".old")
        os.symlink(swap + ".old", swap)
    print(json.dumps({{"txid": "%016x" % part["txid"], "integrity_check": "none"}}))
"""


class Fake:
    """``databases``·``ltx``·``restore`` 응답을 spec 으로 정하는 가짜 litestream."""

    def __init__(self, lab: Path, dbs: list[Path], *, ltx=None, restore=None) -> None:
        self.dir = lab / "fake"
        self.dir.mkdir()
        self.spec_path = self.dir / "spec.json"
        self.log = self.dir / "argv.log"
        self.spec = {
            "dbs": [str(d) for d in dbs],
            "ltx": ltx if ltx is not None else {"txid": 5},
            "restore": restore if restore is not None else {},
        }
        self.save()
        self.bin = self.dir / "litestream"
        self.bin.write_text(
            FAKE.format(python=sys.executable, spec=str(self.spec_path), log=str(self.log))
        )
        self.bin.chmod(0o755)
        self.config = lab / "litestream.yml"
        self.config.write_text("# fake\n")

    def save(self) -> None:
        self.spec_path.write_text(json.dumps(self.spec))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls() if c[0] != "version"]

    def args(self, restore_dir: Path, *extra: str) -> list[str]:
        return [
            "--restore-test",
            "--restore-dir",
            str(restore_dir),
            "--litestream-config",
            str(self.config),
            "--litestream",
            str(self.bin),
            *extra,
        ]


@pytest.fixture
def lab(tmp_path):
    """``app/app.db``(WAL, 10행)·복원 원본 ``source.db``·빈 ``restore/``·가짜 litestream."""
    lab = tmp_path.resolve()
    (lab / "app").mkdir()
    db = make_db(lab / "app" / "app.db")
    source = make_db(lab / "source.db", 12, wal=False)
    (lab / "restore").mkdir()
    fake = Fake(lab, [db], restore={"src": str(source), "txid": 5})
    return lab, db, fake


def work_dirs(restore_dir: Path) -> list[Path]:
    return sorted(restore_dir.iterdir())


# --- 인자 오류는 exit 2 -------------------------------------------------------------------


MODE = ["--restore-test", "--restore-dir", ".", "--litestream-config", "c"]


@pytest.mark.parametrize(
    "args, needle",
    [
        (["--restore-test", "--litestream-config", "c.yml"], "requires --restore-dir"),
        (["--restore-test", "--restore-dir", "."], "requires --litestream-config"),
        (["--restore-dir", "."], "--restore-dir require(s) --restore-test"),
        (["--keep", "--integrity", "quick"], "--integrity, --keep require(s) --restore-test"),
        ([*MODE, "--restore-timeout", "0"], "positive"),
        ([*MODE, "--integrity-timeout", "x"], "number"),
        ([*MODE, "--restore-timeout", "nan"], "positive"),
        (["--restore-test", "--integrity", "fast"], "invalid choice"),
    ],
)
def test_argument_errors_exit_2(tmp_path, args, needle):
    code, out, err = run({"default": sqlite_db(tmp_path / "a.db")}, *args, json_output=False)
    assert code == 2
    assert needle in err
    assert out == ""
    assert not (tmp_path / "a.db").exists()


# --- --restore-dir 검사(아무것도 만들기 전) ---------------------------------------------


def protected(db: Path, meta: Path | None = None, alias="default"):
    meta = meta if meta is not None else ls.default_meta_path(db)
    return [(alias, what, p) for what, p in rc.protected_paths(db, meta)]


def test_restore_dir_must_exist_and_be_a_real_directory(tmp_path):
    lab = tmp_path.resolve()
    (lab / "file").write_text("x")
    (lab / "real").mkdir()
    (lab / "link").symlink_to(lab / "real")
    (lab / "via").symlink_to(lab)
    cases = {
        lab / "missing": "does not exist",
        lab / "file": "is not a directory",
        lab / "link": "is a symlink",
        lab / "via" / "real": "is not a real path",
        f"{lab}/real/../real": "must not contain '..'",
        "/": "filesystem root",
        "": "empty",
        ".": "pass the directory's real absolute path",
        f"{lab}/real/.": "pass the directory's real absolute path",
        f"{lab}/real/..": "pass the directory's real absolute path",
    }
    for raw, needle in cases.items():
        path, problem = rc.check_restore_dir(raw, [])
        assert path is None
        assert needle in problem, (raw, problem)
    assert rc.check_restore_dir(lab / "real", []) == (lab / "real", None)
    assert rc.check_restore_dir(f"{lab}/real/", []) == (lab / "real", None)


def test_restore_dir_overlapping_the_database_directory_is_refused(tmp_path):
    lab = tmp_path.resolve()
    data = lab / "srv" / "data"
    (data / "inside").mkdir(parents=True)
    db = data / "app.db"
    outside = lab / "elsewhere"
    outside.mkdir()
    for raw in (data, data / "inside", lab / "srv", lab):
        path, problem = rc.check_restore_dir(raw, protected(db))
        assert path is None, raw
        assert "overlaps" in problem
    assert rc.check_restore_dir(outside, protected(db)) == (outside, None)


def test_restore_dir_overlapping_a_custom_meta_is_refused(tmp_path):
    lab = tmp_path.resolve()
    (lab / "data").mkdir()
    db = lab / "data" / "app.db"
    meta = lab / "meta" / "app"
    (meta / "ltx").mkdir(parents=True)
    for raw in (meta, meta / "ltx", lab / "meta"):
        path, problem = rc.check_restore_dir(raw, protected(db, meta))
        assert path is None, raw
        assert "litestream meta" in problem
    # 메타가 아직 없어도(대소문자만 다른 이름도) 막는다. 대소문자 무시 볼륨(macOS)에서는
    # lab/GHOST 의 lstat 이 lab/ghost 를 돌려 (st_dev, st_ino) 비교가 먼저 잡으므로, _fold 비교
    # 경로는 대소문자 구분 볼륨(Linux CI)에서만 증명된다.
    ghost = lab / "ghost"
    ghost.mkdir()
    path, problem = rc.check_restore_dir(ghost, protected(db, lab / "GHOST" / "meta"))
    assert path is None
    assert "litestream meta" in problem


def test_restore_dir_inside_the_real_target_of_a_meta_symlink_is_refused(tmp_path):
    lab = tmp_path.resolve()
    (lab / "data").mkdir()
    db = lab / "data" / "app.db"
    real_meta = lab / "volume" / "meta"
    (real_meta / "ltx").mkdir(parents=True)
    link = lab / "meta-link"
    link.symlink_to(real_meta)
    path, problem = rc.check_restore_dir(real_meta / "ltx", protected(db, link))
    assert path is None
    assert "litestream meta (resolved)" in problem


def test_restore_dir_overlapping_another_alias_is_refused(tmp_path):
    lab = tmp_path.resolve()
    for d in ("a", "b"):
        (lab / d).mkdir()
    union = protected(lab / "a" / "app.db") + protected(lab / "b" / "x.db", alias="other")
    path, problem = rc.check_restore_dir(lab / "b", union)
    assert path is None
    assert "alias 'other'" in problem


# --- 무결성 -------------------------------------------------------------------------------


def test_integrity_ok_full_and_quick(tmp_path):
    db = make_db(tmp_path / "ok.db", wal=False)
    for mode, pragma in (("full", "integrity_check"), ("quick", "quick_check")):
        passed, got = rc.check_integrity(db, mode, 30)
        assert passed
        assert [(i.key, i.level) for i in got] == [
            ("integrity", "ok"),
            ("tables", "ok"),
            ("page_count", "ok"),
        ]
        assert got[0].value == mode
        assert got[0].message.startswith(f"{pragma}: ok")
        assert got[1].value == 1


def test_integrity_failure_reports_the_first_lines(tmp_path):
    db = make_corrupt_db(tmp_path / "bad.db")
    passed, got = rc.check_integrity(db, "full", 30)
    assert not passed
    assert (got[0].key, got[0].level) == ("integrity", "error")
    assert got[0].message.startswith("integrity_check: ")
    assert "index i" in got[0].message
    assert got[0].message.count(" | ") <= rc._MAX_PROBLEMS - 1


def test_integrity_open_failure_is_error(tmp_path):
    (tmp_path / "not-sqlite.db").write_bytes(b"x" * 4096)
    passed, got = rc.check_integrity(tmp_path / "not-sqlite.db", "full", 30)
    assert not passed
    assert [(i.key, i.level) for i in got] == [("integrity", "error")]


def test_integrity_other_error_after_the_timer_fired_reports_both(tmp_path, monkeypatch):
    """타이머가 발동했어도 예외가 ``interrupted`` 가 아니면 ``timeout`` 이라고만 하지 않는다."""
    db = make_db(tmp_path / "ok.db", wal=False)

    class Slow:
        def __init__(self, conn):
            self.conn = conn
            self.interrupted = False

        def interrupt(self):
            self.interrupted = True

        def execute(self, sql):
            time.sleep(0.2)
            raise sqlite3.DatabaseError("database disk image is malformed")

        def close(self):
            self.conn.close()

    opened = []
    real_connect = rc._connect_ro

    def connect(path):
        opened.append(Slow(real_connect(path)))
        return opened[-1]

    monkeypatch.setattr(rc, "_connect_ro", connect)
    passed, got = rc.check_integrity(db, "full", 0.01)
    assert not passed
    assert opened[0].interrupted
    assert [(i.key, i.level) for i in got] == [("integrity", "error")]
    message = got[0].message
    assert not message.startswith("timeout")
    assert "database disk image is malformed" in message
    assert "--integrity-timeout 0.01s timer had also fired" in message


def test_integrity_timer_fired_before_the_check_started_is_timeout(tmp_path, monkeypatch):
    """시한이 문장 시작 전에 지나도 ``timeout`` 이다(#60).

    실행 중인 문장이 없을 때의 ``sqlite3_interrupt()`` 는 no-op 이라 검사는 끝까지 돈다
    (SQLite 문서 c3ref/interrupt). 타이머 발동 뒤에야 ``execute`` 에 들어가게 해 그 순서를 강제한다.
    """
    db = make_db(tmp_path / "ok.db", wal=False)

    class Late:
        def __init__(self, conn):
            self.conn = conn
            self.fired = threading.Event()

        def interrupt(self):
            self.conn.interrupt()
            self.fired.set()

        def execute(self, sql):
            assert self.fired.wait(30)
            return self.conn.execute(sql)

        def close(self):
            self.conn.close()

    real_connect = rc._connect_ro
    monkeypatch.setattr(rc, "_connect_ro", lambda path: Late(real_connect(path)))
    passed, got = rc.check_integrity(db, "full", 0.01)
    assert not passed
    assert [(i.key, i.level) for i in got] == [("integrity", "error")]
    message = got[0].message
    assert message.startswith("timeout: integrity_check did not finish within --integrity-timeout")
    assert "interrupted after" not in message
    assert "was not interrupted" in message


def test_integrity_timeout_interrupts_the_check(tmp_path):
    """제한을 넘긴 full 검사가 ``interrupt()`` 로 실제로 끝난다(#54 실측: 1ms 미만 지연)."""
    db = make_big_db(tmp_path / "big.db")
    # 첫 검사는 페이지 캐시가 차가워 느리다(macOS 러너에서 두 번째가 4배 넘게 빨랐다). 데운 뒤 잰다.
    assert rc.check_integrity(db, "full", 600)[0]
    start = time.monotonic()
    passed, _ = rc.check_integrity(db, "full", 600)
    full = time.monotonic() - start
    assert passed
    limit = min(0.005, full / 10)
    # 부하로 타이머가 문장 시작 전에 발동하면 interrupt() 는 no-op 이고 검사는 끝까지 돈다(#60).
    # 그 경로는 'was not interrupted' 로 구분되므로, 실제로 끊긴 경우를 볼 때까지 몇 번 더 잰다.
    for _ in range(5):
        start = time.monotonic()
        passed, got = rc.check_integrity(db, "full", limit)
        elapsed = time.monotonic() - start
        if "was not interrupted" not in got[0].message:
            break
    assert not passed, (full, limit, elapsed)
    assert [(i.key, i.level) for i in got] == [("integrity", "error")]
    # 'interrupted after' 는 타이머가 발동했고 예외가 interrupted 일 때만 나온다(F8): 검사가 끝나기
    # 전에 interrupt() 로 끊겼다는 증거다. 걸린 시간은 스레드 스케줄링 지연만큼 여유를 둔다(macOS
    # 러너에서 데운 full 검사가 50ms 라 full/2 기준은 0.2ms 차이로 흔들렸다).
    assert got[0].message.startswith("timeout: integrity_check did not finish")
    assert "interrupted after" in got[0].message, got[0].message
    assert elapsed < limit + 0.5, (elapsed, limit, full)


# --- 정리 ---------------------------------------------------------------------------------


def test_finish_rules(tmp_path, monkeypatch):
    def work(name):
        d = tmp_path / name
        d.mkdir()
        (d / "app.db").write_text("x")
        return d

    passed = work("passed")
    assert rc._finish("a", passed, True, False) == []
    assert not passed.exists()

    failed = work("failed")
    (out,) = rc._finish("a", failed, False, False)
    assert (out.key, out.level, out.value) == ("output", "ok", str(failed))
    assert failed.exists()

    kept = work("kept")
    (out,) = rc._finish("a", kept, True, True)
    assert (out.level, out.message) == ("ok", "kept (--keep)")
    assert kept.exists()

    def boom(path):
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(rc.shutil, "rmtree", boom)
    stuck = work("stuck")
    (out,) = rc._finish("a", stuck, True, False)
    assert (out.key, out.level, out.value) == ("output", "warn", str(stuck))
    assert "cleanup failed" in out.message


# --- 명령(가짜 litestream) ----------------------------------------------------------------


def test_fake_success_cleans_up_and_opens_no_django_connection(lab):
    lab, db, fake = lab
    before = fingerprint(lab / "app")
    # init_command 가 실행되면(=Django 연결) user_version 이 바뀐다.
    databases = {"default": sqlite_db(db, init_command="PRAGMA user_version=7")}
    code, data, _ = run(databases, *fake.args(lab / "restore"))
    assert code == 0, data
    assert keys(data) == [
        "space",
        "restore_dir",
        "remote_txid",
        "restore",
        "restored_txid",
        "duration_ms",
        "size",
        "integrity",
        "tables",
        "page_count",
    ]
    assert one(data, "remote_txid")["value"] == "0000000000000005"
    assert one(data, "restored_txid")["expected"] == "0000000000000005"
    assert one(data, "integrity")["value"] == "full"
    assert one(data, "size")["value"] == os.path.getsize(lab / "source.db")
    work = Path(one(data, "restore_dir")["value"])
    assert work.parent == lab / "restore"
    assert work.name.startswith("dso-restore-")
    assert work_dirs(lab / "restore") == []
    assert fingerprint(lab / "app") == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
    restore_call = [c for c in fake.calls() if c[0] == "restore"][0]
    assert restore_call[restore_call.index("-integrity-check") + 1] == "none"
    assert restore_call[restore_call.index("-o") + 1] == str(work / "app.db")
    assert restore_call[-1] == str(db)
    assert fake.commands() == ["databases", "ltx", "restore"]


def test_fake_quick_and_keep(lab):
    lab, db, fake = lab
    code, data, _ = run(
        {"default": sqlite_db(db)}, *fake.args(lab / "restore", "--integrity", "quick", "--keep")
    )
    assert code == 0
    assert one(data, "integrity")["value"] == "quick"
    assert "quick_check: ok" in one(data, "integrity")["message"]
    out = one(data, "output")
    assert out["message"] == "kept (--keep)"
    assert work_dirs(lab / "restore") == [Path(out["value"])]
    assert (Path(out["value"]) / "app.db").is_file()


def test_fake_restored_below_remote_warns_and_still_cleans_up(lab):
    lab, db, fake = lab
    fake.spec["ltx"] = {"txid": 9}
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 1
    txid = one(data, "restored_txid")
    assert (txid["level"], txid["value"], txid["expected"]) == (
        "warn",
        "0000000000000005",
        "0000000000000009",
    )
    assert items(data, "output") == []
    assert work_dirs(lab / "restore") == []


def test_fake_restored_above_remote_is_ok(lab):
    """복원 중 새 업로드로 복원 TXID 가 원격보다 크면 정상이다(D-22 '이상')."""
    lab, db, fake = lab
    fake.spec["restore"]["txid"] = 9
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 0, data
    txid = one(data, "restored_txid")
    assert (txid["level"], txid["value"], txid["expected"]) == (
        "ok",
        "0000000000000009",
        "0000000000000005",
    )
    assert work_dirs(lab / "restore") == []


def test_fake_remote_error_is_unknown_and_still_restores(lab):
    lab, db, fake = lab
    fake.spec["ltx"] = {"rc": 1, "stderr": "access denied secret-access-key: hunter2"}
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 1
    remote = one(data, "remote_txid")
    assert remote["level"] == "unknown"
    assert "hunter2" not in remote["message"]
    assert one(data, "restored_txid")["level"] == "unknown"
    assert one(data, "integrity")["level"] == "ok"
    assert work_dirs(lab / "restore") == []


def test_fake_empty_replica_is_error_and_keeps_the_work_dir(lab):
    lab, db, fake = lab
    fake.spec["ltx"] = {"txid": None}
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 2
    remote = one(data, "remote_txid")
    assert remote["level"] == "error"
    assert "mistyped replica path" in remote["message"]
    assert "restore" not in keys(data)
    assert work_dirs(lab / "restore") == [Path(one(data, "output")["value"])]
    assert "restore" not in fake.commands()


def test_fake_restore_failure_keeps_output_and_skips_integrity(lab):
    lab, db, fake = lab
    fake.spec["restore"] = {"rc": 1, "stderr": "no matching backup files available"}
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 2
    assert one(data, "restore")["level"] == "error"
    assert "no matching backup files" in one(data, "restore")["message"]
    assert "integrity" not in keys(data)
    assert "restored_txid" not in keys(data)
    assert work_dirs(lab / "restore") == [Path(one(data, "output")["value"])]


def test_fake_restore_timeout(lab):
    lab, db, fake = lab
    fake.spec["restore"] = {"sleep": 30}
    fake.save()
    start = time.monotonic()
    code, data, _ = run(
        {"default": sqlite_db(db)}, *fake.args(lab / "restore", "--restore-timeout", "1")
    )
    assert time.monotonic() - start < 20
    assert code == 2
    assert "timed out after 1s" in one(data, "restore")["message"]
    assert "integrity" not in keys(data)
    assert len(work_dirs(lab / "restore")) == 1


def test_fake_corrupt_copy_fails_integrity_and_is_kept(lab):
    lab, db, fake = lab
    fake.spec["restore"] = {"src": str(make_corrupt_db(lab / "bad.db")), "txid": 5}
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 2
    assert one(data, "restore")["level"] == "ok"
    assert one(data, "integrity")["level"] == "error"
    out = Path(one(data, "output")["value"])
    assert work_dirs(lab / "restore") == [out]
    assert (out / "app.db").is_file()


def test_fake_integrity_timeout_exit_2_and_kept(lab):
    lab, db, fake = lab
    fake.spec["restore"] = {"src": str(make_big_db(lab / "big.db")), "txid": 5}
    fake.save()
    code, data, _ = run(
        {"default": sqlite_db(db)}, *fake.args(lab / "restore", "--integrity-timeout", "0.005")
    )
    assert code == 2
    integrity = one(data, "integrity")
    assert integrity["level"] == "error"
    assert integrity["message"].startswith("timeout: integrity_check")
    assert "tables" not in keys(data)
    assert work_dirs(lab / "restore") == [Path(one(data, "output")["value"])]


def test_fake_not_enough_space_creates_nothing(lab):
    lab, db, fake = lab
    # 필요량에 -wal 크기도 들어간다(doctor 는 운영 DB 를 열지 않으므로 내용은 상관없다).
    Path(f"{db}-wal").write_bytes(b"\0" * 5000)
    post = (
        "import shutil\n"
        "shutil.disk_usage = lambda p: shutil._ntuple_diskusage(10**12, 10**12 - 100, 100)\n"
    )
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"), post=post)
    assert code == 2
    space = one(data, "space")
    assert (space["level"], space["value"]) == ("error", 100)
    local = os.path.getsize(db) + os.path.getsize(f"{db}-wal")
    assert space["expected"] == -(-local * 12 // 10)
    assert "estimated from the local db + -wal size" in space["message"]
    assert keys(data) == ["space"]
    assert work_dirs(lab / "restore") == []
    assert fake.commands() == ["databases"]


def test_fake_restore_dir_problems_stop_before_litestream(lab):
    lab, db, fake = lab
    meta = ls.default_meta_path(db)
    (meta / "ltx" / "0").mkdir(parents=True)
    (lab / "to-meta").symlink_to(meta)
    before = fingerprint(lab / "app")
    for target in (lab / "app", meta, lab / "to-meta", lab, lab / "missing"):
        code, data, _ = run({"default": sqlite_db(db)}, *fake.args(target))
        assert code == 2, target
        item = one(data, "restore_dir", alias=None)
        assert item["level"] == "error"
    assert fingerprint(lab / "app") == before
    assert fake.commands() == []


def test_fake_health_meta_path_is_protected(lab):
    lab, db, fake = lab
    meta = lab / "meta"
    meta.mkdir()
    entry = {"litestream_config": str(fake.config), "meta_path": str(meta)}
    health = {"HEALTH": {"DATABASES": {"default": entry}}}
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(meta), SQLITE_OPS=health)
    assert code == 2
    assert "litestream meta" in one(data, "restore_dir", alias=None)["message"]
    assert list(meta.iterdir()) == []


def test_fake_invalid_health_settings_refuse(lab):
    lab, db, fake = lab
    health = {"HEALTH": {"DATABASES": {"default": {}}}}
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"), SQLITE_OPS=health)
    assert code == 2
    assert "SQLITE_OPS['HEALTH'] is invalid" in one(data, "restore_dir", alias=None)["message"]
    assert work_dirs(lab / "restore") == []


def test_fake_restore_dir_inside_another_alias_directory(lab):
    lab, db, fake = lab
    (lab / "other").mkdir()
    other = make_db(lab / "other" / "o.db")
    databases = {"default": sqlite_db(db), "other": sqlite_db(other)}
    before = fingerprint(lab / "other")
    # 보호 대상은 고른 별칭과 무관하게 sqlite3 별칭 전부다(D-25, 2026-10-18).
    for extra in (["--database", "default"], []):
        code, data, _ = run(databases, *fake.args(lab / "other", *extra))
        assert code == 2, extra
        assert "alias 'other'" in one(data, "restore_dir", alias=None)["message"]
    assert fake.commands() == []
    assert fingerprint(lab / "other") == before


def test_fake_aliases_that_are_not_write_file_dbs_are_unknown(lab):
    lab, db, fake = lab
    databases = {
        "default": sqlite_db(db),
        "mem": sqlite_db(":memory:"),
        "ro": sqlite_db(f"file:{db}?mode=ro"),
        "pg": NOT_SQLITE,
    }
    code, data, _ = run(
        databases,
        *fake.args(lab / "restore", "--database", "default", "--database", "mem"),
        "--database",
        "ro",
        "--database",
        "pg",
        "--database",
        "nope",
    )
    assert code == 2
    assert one(data, "alias", "mem")["level"] == "unknown"
    assert one(data, "alias", "ro")["value"] == "read-only"
    assert one(data, "alias", "pg")["level"] == "unknown"
    assert one(data, "alias", "nope")["level"] == "error"
    assert one(data, "integrity")["level"] == "ok"


def _bogus_option(other: Path) -> dict:
    # Django 6.1 의 get_connection_params() 가 ImproperlyConfigured 를 낸다(역할 error).
    return sqlite_db(other, transaction_mode="BOGUS")


def _ambiguous_uri(other: Path) -> dict:
    # 역할 키 중복이라 판정할 수 없지만(역할 unknown) 표준 sqlite3 는 이 URI 로 쓰기 연결을 연다.
    return sqlite_db(f"file:{other}?mode=rwc&mode=rwc", uri=True)


@pytest.mark.parametrize("extra", [["--database", "default"], []], ids=["selected", "default"])
@pytest.mark.parametrize(
    "config, role",
    [(_bogus_option, "error"), (_ambiguous_uri, "unknown")],
    ids=["params-fail", "uri-undeterminable"],
)
def test_fake_alias_with_unknown_paths_refuses_the_restore_dir(lab, extra, config, role):
    """경로를 알 수 없는 sqlite3 별칭이 있으면 보호 합집합이 불완전하므로 거부한다(R2-1·R3-1)."""
    lab, db, fake = lab
    (lab / "other").mkdir()
    other = make_db(lab / "other" / "o.db")
    databases = {"default": sqlite_db(db), "other": config(other)}
    before = fingerprint(lab / "other")
    code, data, _ = run(databases, *fake.args(lab / "other", *extra))
    assert code == 2, data
    item = one(data, "restore_dir", alias=None)
    assert item["level"] == "error"
    assert "cannot determine the paths of alias 'other'" in item["message"]
    assert f"role {role}" in item["message"]
    assert items(data, "restore") == []
    assert fake.commands() == []
    assert fingerprint(lab / "other") == before


SUBCLASS_BACKEND = """
from django.db.backends.sqlite3 import base


class DatabaseWrapper(base.DatabaseWrapper):
    pass
"""


@pytest.mark.parametrize("extra", [["--database", "default"], []], ids=["selected", "default"])
def test_fake_sqlite_subclass_backend_is_protected(lab, extra):
    """ENGINE 이 sqlite3 와 다른 SQLite 백엔드(vendor sqlite)의 경로도 보호한다(R4-1)."""
    lab, db, fake = lab
    package = lab / "pkg" / "mybackend"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "base.py").write_text(SUBCLASS_BACKEND)
    (lab / "other").mkdir()
    other = make_db(lab / "other" / "o.db")
    databases = {"default": sqlite_db(db), "other": {"ENGINE": "mybackend", "NAME": str(other)}}
    before = fingerprint(lab / "other")
    post = f"import sys\nsys.path.insert(0, {str(lab / 'pkg')!r})\n"
    code, data, _ = run(databases, *fake.args(lab / "other", *extra), post=post)
    assert code == 2, data
    item = one(data, "restore_dir", alias=None)
    assert "overlaps" in item["message"]
    assert "alias 'other'" in item["message"]
    assert fake.commands() == []
    assert fingerprint(lab / "other") == before
    # 실행 대상은 지금처럼 ENGINE 일치로 고른다: 겹치지 않는 DIR 이면 default 만 복원한다.
    code, data, _ = run(databases, *fake.args(lab / "restore", *extra), post=post)
    assert code == 0, data
    assert [c[-1] for c in fake.calls() if c[0] == "restore"] == [str(db)]


@pytest.mark.parametrize("extra", [["--database", "default"], []], ids=["selected", "default"])
def test_fake_unloadable_backend_refuses_the_restore_dir(lab, extra):
    """백엔드를 불러오지 못하는 별칭은 SQLite 인지 알 수 없으므로 거부한다(R4-1)."""
    lab, db, fake = lab
    databases = {"default": sqlite_db(db), "x": {"ENGINE": "no_such_backend_dso", "NAME": "x"}}
    code, data, _ = run(databases, *fake.args(lab / "restore", *extra))
    assert code == 2, data
    item = one(data, "restore_dir", alias=None)
    assert "cannot load the database backend of alias 'x'" in item["message"]
    assert "no_such_backend_dso" in item["message"]
    assert fake.commands() == []
    assert work_dirs(lab / "restore") == []


def _db_behind_a_file_link(lab: Path) -> tuple[str, Path, Path]:
    """``a/app.db`` → ``vol/b/app.db``: SQLite 는 링크를 따라가 실체 옆에 -wal·-shm 을 만든다."""
    (lab / "vol" / "b" / "sub").mkdir(parents=True)
    real = make_db(lab / "vol" / "b" / "app.db")
    (lab / "a").mkdir()
    (lab / "a" / "app.db").symlink_to(real)
    return str(lab / "a" / "app.db"), lab / "vol" / "b", lab / "vol" / "b" / "sub"


def _db_behind_dotdot_after_a_link(lab: Path) -> tuple[str, Path, Path]:
    """``srv/x/../data/app.db`` 이고 ``srv/x`` → ``vol/deep/x``: 실체는 ``vol/deep/data/app.db``."""
    (lab / "vol" / "deep" / "x").mkdir(parents=True)
    (lab / "vol" / "deep" / "data").mkdir()
    make_db(lab / "vol" / "deep" / "data" / "app.db")
    (lab / "srv").mkdir()
    (lab / "srv" / "x").symlink_to(lab / "vol" / "deep" / "x")
    real_dir = lab / "vol" / "deep" / "data"
    return f"{lab}/srv/x/../data/app.db", real_dir, real_dir


@pytest.mark.parametrize("extra", [["--database", "default"], []], ids=["selected", "default"])
@pytest.mark.parametrize(
    "layout", [_db_behind_a_file_link, _db_behind_dotdot_after_a_link], ids=["file-link", "dotdot"]
)
def test_fake_real_location_behind_links_is_protected(lab, extra, layout):
    """링크를 따라간 실제 DB 위치(부모 디렉터리·사이드카)도 보호한다(리뷰 5 R5-1)."""
    lab, db, fake = lab
    name, real_dir, restore_dir = layout(lab)
    databases = {"default": sqlite_db(db), "other": sqlite_db(name)}
    before = fingerprint(real_dir)
    code, data, _ = run(databases, *fake.args(restore_dir, *extra))
    assert code == 2, data
    item = one(data, "restore_dir", alias=None)
    assert "alias 'other'" in item["message"], item
    assert fake.commands() == []
    assert fingerprint(real_dir) == before


def test_fake_database_file_that_is_a_symlink_is_error(lab):
    """실행 대상의 DB 파일이 링크면 boot(D-15)와 같이 거부한다. 다른 별칭은 그대로 돈다(R5-1)."""
    lab, db, fake = lab
    name, _, _ = _db_behind_a_file_link(lab)
    databases = {"default": sqlite_db(db), "linked": sqlite_db(name)}
    code, data, _ = run(databases, *fake.args(lab / "restore"))
    assert code == 2, data
    item = one(data, "database", "linked")
    assert item["level"] == "error"
    assert "symlink" in item["message"]
    assert one(data, "integrity")["level"] == "ok"
    assert [c[-1] for c in fake.calls() if c[0] == "restore"] == [str(db)]


def test_fake_dir_entry_in_the_litestream_config_is_refused(lab):
    """``dir:`` 항목이 있으면 감시 디렉터리를 모르므로 아무것도 만들기 전에 거부한다(R5-2, D-26)."""
    lab, db, fake = lab
    fake.spec["dir_entry"] = True
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 2, data
    item = one(data, "config", alias=None)
    assert item["level"] == "error"
    assert "dir: entry" in item["message"]
    assert fake.commands() == ["databases"]
    assert work_dirs(lab / "restore") == []


def test_fake_relative_path_in_the_litestream_config_is_refused(lab):
    """상대 ``path:`` 는 replicate 의 cwd 에 따라 위치가 바뀌므로 거부한다(R6-1, D-26)."""
    lab, db, fake = lab
    fake.spec["relative"] = ["data/app.db"]
    fake.save()
    code, data, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"))
    assert code == 2, data
    item = one(data, "config", alias=None)
    assert item["level"] == "error"
    assert "relative path" in item["message"]
    assert "dir: entry" not in item["message"]
    assert item["message"].endswith(
        "/data/app.db in the litestream config; its location depends "
        "on where litestream runs, so the paths to protect are "
        "unknown (D-26)"
    )
    assert fake.commands() == ["databases"]
    assert work_dirs(lab / "restore") == []


def test_fake_alias_with_a_nul_in_its_path_refuses_the_restore_dir(lab):
    """NAME 에 NUL 이 든 별칭은 보호 경로를 정할 수 없으므로 거부한다(R6-2)."""
    lab, db, fake = lab
    databases = {"default": sqlite_db(db), "weird": sqlite_db(str(lab / "app" / "x\0y.db"))}
    code, data, _ = run(databases, *fake.args(lab / "restore"))
    assert code == 2, data
    item = one(data, "restore_dir", alias=None)
    assert item["level"] == "error"
    assert "cannot determine the paths of alias 'weird'" in item["message"]
    assert "null" in item["message"]
    assert items(data, "restore") == []
    assert fake.commands() == []
    assert work_dirs(lab / "restore") == []


def test_fake_restore_dir_swapped_after_the_check_is_refused(lab):
    """검사 뒤 ``--restore-dir`` 이 링크로 바뀌면 다음 별칭의 mkdtemp 전에 멈춘다(R5-4)."""
    lab, db, fake = lab
    (lab / "b").mkdir()
    second = make_db(lab / "b" / "b.db")
    fake.spec["dbs"].append(str(second))
    fake.spec["restore"]["swap"] = str(lab / "restore")
    fake.save()
    databases = {"default": sqlite_db(db), "b": sqlite_db(second)}
    code, data, _ = run(databases, *fake.args(lab / "restore"))
    assert code == 2, data
    assert one(data, "integrity")["level"] == "ok"
    item = one(data, "restore_dir", "b")
    assert item["level"] == "error"
    assert "changed since it was checked" in item["message"]
    assert [c[-1] for c in fake.calls() if c[0] == "restore"] == [str(db)]
    assert list((lab / "restore.old").iterdir()) == []


def test_fake_explicit_non_write_alias_only_is_error(lab):
    """``--database`` 로 고른 별칭이 읽기 전용뿐이면 unknown 과 함께 NO_TARGET error(exit 2)."""
    lab, db, fake = lab
    databases = {"default": sqlite_db(db), "ro": sqlite_db(f"file:{db}?mode=ro")}
    code, data, _ = run(databases, *fake.args(lab / "restore", "--database", "ro"))
    assert code == 2, data
    assert one(data, "alias", "ro")["level"] == "unknown"
    item = one(data, "alias", alias=None)
    assert (item["level"], item["message"]) == ("error", rc.NO_TARGET)
    assert fake.commands() == []
    assert work_dirs(lab / "restore") == []


def test_fake_default_selection_skips_aliases_that_are_not_write_file_dbs(lab):
    """``--database`` 가 없으면 쓰기 파일 DB 만 실행하고, 나머지는 ok 'skipped' 로만 남긴다."""
    lab, db, fake = lab
    databases = {
        "default": sqlite_db(db),
        "mem": sqlite_db(":memory:"),
        "ro": sqlite_db(f"file:{db}?mode=ro"),
        "pg": NOT_SQLITE,
    }
    code, data, _ = run(databases, *fake.args(lab / "restore"))
    assert code == 0, data
    for alias, role in (("mem", "memory"), ("ro", "read-only")):
        skipped = one(data, "alias", alias)
        assert (skipped["level"], skipped["value"]) == ("ok", role), skipped
        assert skipped["message"].startswith(f"skipped ({role})")
        assert keys(data, alias) == ["alias"]
    assert items(data, alias="pg") == []
    assert one(data, "integrity")["level"] == "ok"
    assert [c[-1] for c in fake.calls() if c[0] == "restore"] == [str(db)]


@pytest.mark.parametrize(
    "databases",
    [
        {"default": NOT_SQLITE},
        {
            "default": NOT_SQLITE,
            "mem": sqlite_db(":memory:"),
        },
    ],
    ids=["no-sqlite-alias", "only-non-write-alias"],
)
def test_fake_nothing_to_restore_test_is_error(lab, databases):
    """실행한 쓰기 DB 가 없으면 아무것도 검증하지 않은 성공(exit 0)이 아니라 error 다(리뷰 1 F1)."""
    lab, _, fake = lab
    args = fake.args(lab / "restore")
    args[args.index("--litestream-config") + 1] = str(lab / "no-such.yml")
    code, data, _ = run(databases, *args)
    assert code == 2, data
    item = one(data, "alias", alias=None)
    assert (item["level"], item["message"]) == ("error", rc.NO_TARGET)
    assert fake.commands() == []
    assert work_dirs(lab / "restore") == []


def test_fake_nothing_to_run_with_another_error_adds_no_extra_item(lab):
    lab, db, fake = lab
    args = fake.args(lab / "restore", "--database", "nope")
    code, data, _ = run({"default": sqlite_db(db)}, *args)
    assert code == 2
    assert one(data, "alias", "nope")["level"] == "error"
    assert items(data, "alias", alias=None) == []


def test_fake_db_not_in_config_or_not_real_path(lab):
    lab, db, fake = lab
    fake.spec["dbs"] = [str(lab / "elsewhere.db")]
    fake.save()
    (lab / "link").symlink_to(lab / "app")
    databases = {"default": sqlite_db(db), "linked": sqlite_db(lab / "link" / "app.db")}
    code, data, _ = run(databases, *fake.args(lab / "restore"))
    assert code == 2
    assert "not in the litestream config" in one(data, "database")["message"]
    assert "not a real path" in one(data, "database", "linked")["message"]
    assert "ltx" not in fake.commands()


def test_fake_config_failure(lab):
    lab, db, fake = lab
    code, data, _ = run(
        {"default": sqlite_db(db)},
        *fake.args(lab / "restore"),
        "--litestream",
        str(lab / "no-such-litestream"),
    )
    assert code == 2
    assert one(data, "config", alias=None)["level"] == "error"
    assert work_dirs(lab / "restore") == []


def test_fake_two_aliases_run_in_turn(lab):
    lab, db, fake = lab
    (lab / "b").mkdir()
    second = make_db(lab / "b" / "b.db")
    fake.spec["dbs"].append(str(second))
    fake.save()
    databases = {"default": sqlite_db(db), "b": sqlite_db(second)}
    code, data, _ = run(databases, *fake.args(lab / "restore"))
    assert code == 0, data
    assert one(data, "integrity", "b")["level"] == "ok"
    restores = [c[-1] for c in fake.calls() if c[0] == "restore"]
    assert restores == [str(db), str(second)]


def test_text_output_has_restore_section(lab):
    lab, db, fake = lab
    code, out, _ = run({"default": sqlite_db(db)}, *fake.args(lab / "restore"), json_output=False)
    assert code == 0
    assert out.startswith("[restore]\n")
    assert "integrity" in out
    assert out.rstrip().endswith("-> exit 0")


# --- 실제 litestream ----------------------------------------------------------------------


def replicate(db: Path, config: Path, *, above: int = 0) -> int:
    """replicate 를 돌려 원격 TXID 가 로컬 메타와 같고 ``above`` 보다 클 때까지 기다린 뒤 멈춘다."""
    proc = subprocess.Popen(
        [LITESTREAM, "replicate", "-config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        remote = local = None
        while time.monotonic() < deadline:
            remote = ls.remote_max_txid(db, config=config, binary=LITESTREAM)
            local = ls.local_max_txid(db)
            if type(remote) is ls.RemoteTxid and remote.txid == local and local > above:
                return local
            time.sleep(0.3)
        pytest.fail(f"replica did not catch up: remote={remote} local={local}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def real_args(lab: Path, config: Path, *extra: str) -> list[str]:
    return [
        "--restore-test",
        "--restore-dir",
        str(lab / "restore"),
        "--litestream-config",
        str(config),
        "--litestream",
        LITESTREAM,
        *extra,
    ]


@pytest.fixture
def real(tmp_path):
    """복제를 마친 ``app/app.db`` 와 file 복제본. replicate 는 멈춘 상태다."""
    lab = tmp_path.resolve()
    (lab / "app").mkdir()
    (lab / "restore").mkdir()
    db = make_db(lab / "app" / "app.db", 30)
    config = write_config(lab, db, f"      type: file\n      path: {lab / 'replica'}\n")
    txid = replicate(db, config)
    return lab, db, config, txid


@needs_litestream
def test_real_restore_passes_and_leaves_operational_files_alone(real):
    lab, db, config, txid = real
    assert ls.default_meta_path(db).is_dir()
    with sqlite3.connect(db) as conn:  # 사이드카가 있는 상태로 둔다
        conn.execute("SELECT count(*) FROM t").fetchone()
        before = fingerprint(lab / "app")
        code, data, _ = run({"default": sqlite_db(db)}, *real_args(lab, config))
        after = fingerprint(lab / "app")
    assert code == 0, data
    assert after == before
    assert one(data, "remote_txid")["value"] == f"{txid:016x}"
    assert one(data, "restored_txid")["value"] == f"{txid:016x}"
    assert one(data, "integrity")["level"] == "ok"
    assert one(data, "tables")["value"] >= 1
    assert work_dirs(lab / "restore") == []


@needs_litestream
def test_real_keep_restores_the_same_rows(real):
    lab, db, config, _ = real
    code, data, _ = run({"default": sqlite_db(db)}, *real_args(lab, config, "--keep"))
    assert code == 0
    copy = Path(one(data, "output")["value"]) / "app.db"
    with sqlite3.connect(copy) as conn, sqlite3.connect(db) as orig:
        assert (
            conn.execute("SELECT count(*), max(v) FROM t").fetchone()
            == orig.execute("SELECT count(*), max(v) FROM t").fetchone()
        )


@needs_litestream
def test_real_empty_replica(tmp_path):
    lab = tmp_path.resolve()
    (lab / "app").mkdir()
    (lab / "restore").mkdir()
    db = make_db(lab / "app" / "app.db")
    config = write_config(lab, db, f"      type: file\n      path: {lab / 'replica'}\n")
    code, data, _ = run({"default": sqlite_db(db)}, *real_args(lab, config))
    assert code == 2
    assert one(data, "remote_txid")["level"] == "error"
    assert "restore" not in keys(data)
    assert work_dirs(lab / "restore") == [Path(one(data, "output")["value"])]


@needs_litestream
def test_real_tampered_replica_fails_and_keeps_output(real):
    lab, db, config, _ = real
    ltx_files = sorted((lab / "replica").rglob("*.ltx"))
    assert ltx_files
    for path in ltx_files:
        data = bytearray(path.read_bytes())
        for offset in range(120, len(data), 97):
            data[offset] ^= 0xFF
        path.write_bytes(bytes(data))
    code, data, _ = run({"default": sqlite_db(db)}, *real_args(lab, config))
    assert code == 2
    failed = [i for i in items(data) if i["key"] in ("restore", "integrity")]
    assert failed[-1]["level"] == "error", data
    assert work_dirs(lab / "restore") == [Path(one(data, "output")["value"])]


@needs_litestream
def test_real_unreachable_endpoint_times_out(tmp_path):
    lab = tmp_path.resolve()
    (lab / "app").mkdir()
    (lab / "restore").mkdir()
    db = make_db(lab / "app" / "app.db")
    with SilentServer() as server:
        config = write_config(
            lab,
            db,
            "      type: s3\n"
            "      bucket: lab\n"
            "      path: app\n"
            f"      endpoint: http://127.0.0.1:{server.port}\n"
            "      region: us-east-1\n"
            "      access-key-id: x\n"
            "      secret-access-key: y\n"
            "      force-path-style: true\n",
        )
        start = time.monotonic()
        code, data, _ = run(
            {"default": sqlite_db(db)},
            *real_args(lab, config, "--restore-timeout", "2"),
            post="import django_sqlite_ops.restore_check as rc\nrc._LTX_TIMEOUT = 2\n",
        )
        elapsed = time.monotonic() - start
    assert code == 2
    assert one(data, "remote_txid")["level"] == "unknown"
    assert "timed out after 2s" in one(data, "remote_txid")["message"]
    assert "timed out after 2s" in one(data, "restore")["message"]
    assert elapsed < 30
    assert len(work_dirs(lab / "restore")) == 1


@needs_litestream
def test_real_integrity_timeout(tmp_path):
    lab = tmp_path.resolve()
    (lab / "app").mkdir()
    (lab / "restore").mkdir()
    db = make_big_db(lab / "app" / "app.db")
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    config = write_config(lab, db, f"      type: file\n      path: {lab / 'replica'}\n")
    replicate(db, config)
    code, data, _ = run(
        {"default": sqlite_db(db)}, *real_args(lab, config, "--integrity-timeout", "0.005")
    )
    assert code == 2
    assert one(data, "restore")["level"] == "ok"
    assert one(data, "integrity")["message"].startswith("timeout: ")
    assert work_dirs(lab / "restore") == [Path(one(data, "output")["value"])]


@needs_litestream
def test_real_not_enough_space(real):
    lab, db, config, _ = real
    post = "import shutil\nshutil.disk_usage = lambda p: shutil._ntuple_diskusage(1, 1, 0)\n"
    code, data, _ = run({"default": sqlite_db(db)}, *real_args(lab, config), post=post)
    assert code == 2
    assert one(data, "space")["level"] == "error"
    assert work_dirs(lab / "restore") == []


@needs_litestream
def test_real_dir_entry_in_the_config_is_refused(real):
    """실제 0.5.17: ``dir:`` 항목은 cwd 로 나열된다. 그것을 보고 거부한다(R5-2)."""
    lab, db, config, _ = real
    (lab / "tenants").mkdir()
    config.write_text(
        config.read_text()
        + f"  - dir: {lab / 'tenants'}\n"
        + '    pattern: "*.db"\n'
        + "    recursive: true\n"
        + "    watch: true\n"
        + "    replica:\n"
        + "      type: file\n"
        + f"      path: {lab / 'replica-tenants'}\n"
    )
    code, data, _ = run({"default": sqlite_db(db)}, *real_args(lab, config))
    assert code == 2, data
    assert "dir: entry" in one(data, "config", alias=None)["message"]
    assert "restore" not in keys(data)
    assert work_dirs(lab / "restore") == []
    assert list((lab / "tenants").iterdir()) == []


@needs_litestream
def test_real_relative_path_in_the_config_is_refused(real):
    """실제 0.5.17: 상대 ``path:`` 는 cwd 아래로 나열된다. 그것을 보고 거부한다(R6-1)."""
    lab, db, config, _ = real
    config.write_text(
        config.read_text()
        + "  - path: data/other.db\n"
        + "    replica:\n"
        + "      type: file\n"
        + f"      path: {lab / 'replica-other'}\n"
    )
    code, data, _ = run({"default": sqlite_db(db)}, *real_args(lab, config))
    assert code == 2, data
    message = one(data, "config", alias=None)["message"]
    assert "relative path" in message
    assert message.split(" in the litestream config")[0].endswith(f"{os.sep}data{os.sep}other.db")
    assert "restore" not in keys(data)
    assert work_dirs(lab / "restore") == []


@needs_litestream
def test_real_restore_dir_symlink_to_meta_is_refused_before_anything(real):
    """리뷰 1 재현 사례: ``DIR`` 이 운영 메타를 가리키는 링크면 mkdtemp 가 메타 안에 만들었다."""
    lab, db, config, _ = real
    meta = ls.default_meta_path(db)
    (lab / "restore-link").symlink_to(meta)
    before = fingerprint(lab / "app")
    args = real_args(lab, config)
    args[args.index("--restore-dir") + 1] = str(lab / "restore-link")
    code, data, _ = run({"default": sqlite_db(db)}, *args)
    assert code == 2
    assert "is a symlink" in one(data, "restore_dir", alias=None)["message"]
    assert fingerprint(lab / "app") == before
    for target in (lab / "app", meta, lab):
        args[args.index("--restore-dir") + 1] = str(target)
        code, data, _ = run({"default": sqlite_db(db)}, *args)
        assert code == 2
        assert "overlaps" in one(data, "restore_dir", alias=None)["message"]
    assert fingerprint(lab / "app") == before


def test_shutil_is_the_module_the_check_uses():
    # 가짜 disk_usage 를 주입하는 테스트의 전제.
    assert rc.shutil is shutil


def test_the_connection_probe_sees_a_django_connection(tmp_path):
    """``run()`` 의 'Django 연결을 열지 않았다' 검사가 실제 연결을 잡는지(일반 doctor 는 연결)."""
    db = make_db(tmp_path.resolve() / "a.db")
    config = {"DATABASES": {"default": sqlite_db(db)}}
    argv = [sys.executable, "-c", RUN_SCRIPT, json.dumps(config), "", "--json"]
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
    assert 'OPENED=["default"]' in result.stderr, result.stderr
