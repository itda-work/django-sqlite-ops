"""boot CLI 테스트 (DESIGN §4-2, §4-5, §10 L1–L7).

- 단위: 단계 함수(잠금, 격리·재개, 임시 복원·설치, 무결성, 상태 파일, 인자·종료 코드).
  가짜 ``litestream`` 실행 파일(테스트가 만든 셸 스크립트)로 원격·복원 결과를 조종한다.
- 서브프로세스: ``python -m django_sqlite_ops.boot`` 를 실제로 실행해 exec·잠금 상속을 본다.
- L 시나리오: 실제 ``litestream`` 과 ``file://`` 복제본. 바이너리가 없으면 skip,
  ``REQUIRE_LITESTREAM=1`` 이면 실패(``_litestream.py``).
"""

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _litestream import LITESTREAM, SilentServer, require_litestream, write_config
from _nodjango import ROOT, assert_runs_without_django

from django_sqlite_ops.boot import cli
from django_sqlite_ops.boot import litestream as ls

# 격리 순서대로 놓은 볼륨 파일(-journal 은 WAL 볼륨에 없다).
VOLUME_NAMES = ("app.db", "app.db-wal", "app.db-shm", ".app.db-litestream")

# exec 된 명령 대신 쓰는 스크립트. argv 와 잠금 상속 여부를 마커 파일에 쓴다.
# 새로 연 fd 로 flock 이 실패하면 잠금이 이 프로세스(상속받은 fd)에 걸려 있다는 뜻이다.
MARKER_SCRIPT = """
import fcntl, json, os, sys, time
marker, lock = sys.argv[1], sys.argv[2]
fd = os.open(lock, os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    held = False
except OSError:
    held = True
with open(marker + ".tmp", "w") as f:
    json.dump({"argv": sys.argv[3:], "lock_held": held}, f)
os.rename(marker + ".tmp", marker)
if "--sleep" in sys.argv:
    time.sleep(60)
"""


def marker_command(lab: Path, db: Path) -> list[str]:
    return [sys.executable, "-c", MARKER_SCRIPT, str(lab / "marker.json"), str(cli.lock_path(db))]


def read_marker(lab: Path) -> dict | None:
    path = lab / "marker.json"
    return json.loads(path.read_text()) if path.exists() else None


def run_boot(
    args: list[str], *, timeout: float = 120, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "django_sqlite_ops.boot", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
        check=False,
    )


def make_db(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def count_rows(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def snapshot(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, tuple[str, int]]:
    """``root`` 아래 파일의 (sha256, mtime_ns). 디렉터리는 경로만 남는다."""
    result: dict[str, tuple[str, int]] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        st = os.lstat(p)
        if stat.S_ISREG(st.st_mode):
            result[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns)
        else:
            result[rel] = ("dir", 0)
    return result


# --- 가짜 litestream -------------------------------------------------------------------------


class Fake:
    """가짜 litestream. ``ltx`` 출력·rc 와 ``restore`` 결과를 파일로 조종한다."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "fake"
        self.dir.mkdir()
        self.binary = self.dir / "litestream"
        d = self.dir
        self.binary.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{d}/argv.log"\n'
            'case "$1" in\n'
            "  version) echo 0.5.17; exit 0 ;;\n"
            f'  ltx) cat "{d}/ltx.out"; exit "$(cat "{d}/ltx.rc")" ;;\n'
            "  restore)\n"
            '    out=""; prev=""\n'
            '    for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
            f'    if [ -f "{d}/restore.fail" ]; then echo "Error: boom" >&2; exit 1; fi\n'
            f'    cp "{d}/restore.db" "$out"\n'
            f'    [ -f "{d}/restore.extra" ] && cp "{d}/restore.db" "$out-wal"\n'
            '    echo \'{"txid": "0000000000000005"}\'; exit 0 ;;\n'
            "esac\n"
            "exit 9\n"
        )
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.remote_txid(5)
        make_db(d / "restore.db", 7)

    def remote_txid(self, txid: int | None) -> None:
        items = (
            []
            if txid is None
            else [{"level": 0, "min_txid": f"{1:016x}", "max_txid": f"{txid:016x}"}]
        )
        (self.dir / "ltx.out").write_text(json.dumps(items))
        (self.dir / "ltx.rc").write_text("0")

    def remote_error(self) -> None:
        (self.dir / "ltx.out").write_text("")
        (self.dir / "ltx.rc").write_text("1")

    def fail_restore(self) -> None:
        (self.dir / "restore.fail").touch()

    def calls(self) -> list[str]:
        path = self.dir / "argv.log"
        return [ln.split()[0] for ln in path.read_text().splitlines()] if path.exists() else []


LATEST_LTX = next(
    (
        Path(__file__).parent
        / "fixtures"
        / "litestream-0.5.17"
        / "local_meta_after_replicate"
        / "meta"
        / "ltx"
        / "0"
    ).iterdir()
)  # 0x19-0x19


def write_meta(db: Path) -> Path:
    """로컬 메타에 실측 LTX(최대 TXID 0x19)를 둔다."""
    meta = ls.default_meta_path(db)
    l0 = meta / "ltx" / "0"
    l0.mkdir(parents=True)
    shutil.copy(LATEST_LTX, l0 / LATEST_LTX.name)
    return meta


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    d = tmp_path.resolve() / "data"
    d.mkdir()
    return d


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    return Fake(tmp_path.resolve())


@pytest.fixture
def no_exec(monkeypatch):
    """in-process ``main()`` 이 pytest 를 교체하지 않도록 exec 를 기록만 한다."""
    calls: list[list[str]] = []

    class Execd(Exception):
        pass

    def fake_execvp(file, args):
        calls.append(list(args))
        raise Execd

    monkeypatch.setattr(os, "execvp", fake_execvp)
    return calls, Execd


def boot_args(lab: Path, fake: Fake, *extra: str) -> list[str]:
    return [
        "--db",
        str(lab / "app.db"),
        "--config",
        str(lab / "ls.yml"),
        "--litestream",
        str(fake.binary),
        *extra,
        "--",
        "true",
    ]


def main_inproc(args: list[str], no_exec) -> int | str:
    calls, execd = no_exec
    try:
        return cli.main(args)
    except execd:
        return "exec"


# --- 인자 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--db", "a.db", "--config", "c.yml"],
        ["--db", "a.db", "--config", "c.yml", "--"],
        ["--config", "c.yml", "--", "true"],
        ["--db", "a.db", "--config", "c.yml", "--on-unknown", "yes", "--", "true"],
        ["--db", "a.db", "--config", "c.yml", "--ltx-timeout", "0", "--", "true"],
        ["--db", "a.db", "--config", "c.yml", "--restore-timeout", "nan", "--", "true"],
        ["--db", "a.db", "--config", "c.yml", "--bogus", "--", "true"],
    ],
)
def test_usage_errors_exit_64(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(argv)
    assert exc.value.code == cli.EXIT_USAGE
    assert "usage:" in capsys.readouterr().err


def test_parse_args_defaults_and_command():
    args, command = cli.parse_args(["--db", "/d/app.db", "--config", "c.yml", "--", "a", "--", "b"])
    assert command == ["a", "--", "b"]
    assert args.meta_path == Path("/d/.app.db-litestream")
    assert args.on_unknown == "refuse"
    assert args.ltx_timeout == ls.DEFAULT_LTX_TIMEOUT
    assert args.restore_timeout == ls.DEFAULT_RESTORE_TIMEOUT
    assert not args.adopt_existing and not args.init_new
    args, _ = cli.parse_args(
        ["--db", "a.db", "--config", "c", "--meta-path", "/m", "--ltx-timeout", "2.5", "--", "x"]
    )
    assert args.meta_path == Path("/m")
    assert args.ltx_timeout == 2.5


def test_usage_error_from_subprocess_is_64(lab):
    result = run_boot(["--db", str(lab / "app.db"), "--config", "c.yml"])
    assert result.returncode == 64
    assert "missing command after '--'" in result.stderr


# --- 잠금 ----------------------------------------------------------------------------------


def test_lock_is_exclusive_and_not_inheritable_by_default(lab):
    db = lab / "app.db"
    fd = cli.acquire_lock(db)
    try:
        assert not os.get_inheritable(fd)
        with pytest.raises(cli._Exit) as exc:
            cli.acquire_lock(db)
        assert exc.value.code == cli.EXIT_LOCK
        assert "held by another boot" in exc.value.message
    finally:
        os.close(fd)
    os.close(cli.acquire_lock(db))


def test_lock_in_missing_directory_is_lock_failure(lab):
    with pytest.raises(cli._Exit) as exc:
        cli.acquire_lock(lab / "missing" / "app.db")
    assert exc.value.code == cli.EXIT_LOCK


def test_lock_held_elsewhere_exits_5_without_touching_anything(lab, fake, no_exec):
    db = lab / "app.db"
    fd = os.open(cli.lock_path(db), os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert main_inproc(boot_args(lab, fake), no_exec) == cli.EXIT_LOCK
    finally:
        os.close(fd)
    assert fake.calls() == []


# --- 격리·재개 -------------------------------------------------------------------------------


def local_volume(lab: Path) -> tuple[Path, Path]:
    db = lab / "app.db"
    make_db(db, 3)
    for suffix in cli.SIDECAR_SUFFIXES:
        Path(f"{db}{suffix}").write_bytes(suffix.encode())
    meta = write_meta(db)
    return db, meta


def stale_dirs(lab: Path) -> list[Path]:
    return sorted(p for p in lab.iterdir() if ".stale-" in p.name)


def test_quarantine_targets_order():
    db, meta = Path("/d/app.db"), Path("/d/.app.db-litestream")
    assert cli.quarantine_targets(db, meta) == [
        db,
        Path("/d/app.db-wal"),
        Path("/d/app.db-shm"),
        Path("/d/app.db-journal"),
        meta,
    ]


def test_quarantine_moves_everything_into_one_dir(lab):
    db, meta = local_volume(lab)
    before = snapshot(lab)
    final = cli.quarantine(db, meta)
    assert final is not None and final.is_dir()
    assert final.name.startswith("app.db.stale-") and not final.name.endswith(".partial")
    assert stale_dirs(lab) == [final]
    assert sorted(os.listdir(final)) == sorted(
        [
            ".app.db-litestream",
            "app.db",
            "app.db-journal",
            "app.db-shm",
            "app.db-wal",
            "manifest.json",
        ]
    )
    after = snapshot(final, skip=("manifest.json",))
    assert after == before
    assert [p.name for p in lab.iterdir()] == [final.name]


def test_quarantine_nothing_to_move(lab):
    assert cli.quarantine(lab / "app.db", ls.default_meta_path(lab / "app.db")) is None
    assert list(lab.iterdir()) == []


def crash_on_rename(monkeypatch, n: int) -> None:
    real = os.rename
    count = [0]

    def rename(src, dst, *a, **kw):
        count[0] += 1
        if count[0] == n:
            raise KeyboardInterrupt(f"crash before rename {n}")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", rename)


# 격리 rename: manifest(1), db(2), wal(3), shm(4), journal(5), meta(6), partial→final(7)
@pytest.mark.parametrize("n", range(1, 8))
def test_resume_after_crash_before_each_rename(lab, monkeypatch, n):
    db, meta = local_volume(lab)
    before = snapshot(lab)
    with monkeypatch.context() as m:
        crash_on_rename(m, n)
        with pytest.raises(KeyboardInterrupt):
            cli.quarantine(db, meta)
    partials = [p for p in lab.iterdir() if p.name.endswith(".partial")]
    assert len(partials) == 1
    cli.resume_quarantine(db)
    assert not [p for p in lab.iterdir() if p.name.endswith(".partial")]
    finals = stale_dirs(lab)
    if n == 1:
        # manifest 를 쓰기 전에 죽었다: 옮긴 것이 없으므로 .partial 만 치운다.
        assert finals == []
        assert snapshot(lab) == before
        return
    assert len(finals) == 1
    assert snapshot(finals[0], skip=("manifest.json",)) == before
    assert [p.name for p in lab.iterdir()] == [finals[0].name]
    # 다시 불러도 아무 일도 없다(멱등).
    cli.resume_quarantine(db)
    assert stale_dirs(lab) == finals


def test_resume_follows_manifest_not_current_meta_path(lab, monkeypatch):
    db = lab / "app.db"
    make_db(db, 1)
    meta = lab / "custom-meta"
    meta.mkdir()
    with monkeypatch.context() as m:
        crash_on_rename(m, 3)  # db 를 옮긴 뒤, meta 전에
        with pytest.raises(KeyboardInterrupt):
            cli.quarantine(db, meta)
    cli.resume_quarantine(db)  # 재개는 인자의 meta 경로를 받지 않는다
    (final,) = stale_dirs(lab)
    assert sorted(os.listdir(final)) == ["app.db", "custom-meta", "manifest.json"]


def test_two_partials_refuse(lab):
    db = lab / "app.db"
    (lab / "app.db.stale-20260101T000000.000001Z.partial").mkdir()
    (lab / "app.db.stale-20260101T000000.000002Z.partial").mkdir()
    with pytest.raises(cli._Exit) as exc:
        cli.resume_quarantine(db)
    assert exc.value.code == cli.EXIT_REFUSE


def test_partial_without_manifest_but_with_files_refuses(lab):
    db = lab / "app.db"
    partial = lab / "app.db.stale-20260101T000000.000001Z.partial"
    partial.mkdir()
    (partial / "app.db").write_bytes(b"x")
    with pytest.raises(cli._Exit) as exc:
        cli.resume_quarantine(db)
    assert exc.value.code == cli.EXIT_REFUSE
    assert (partial / "app.db").exists()


def test_resume_conflict_refuses(lab, monkeypatch):
    db, meta = local_volume(lab)
    with monkeypatch.context() as m:
        crash_on_rename(m, 3)
        with pytest.raises(KeyboardInterrupt):
            cli.quarantine(db, meta)
    make_db(db, 1)  # 누군가 그 사이에 새 DB 를 만들었다
    with pytest.raises(cli._Exit) as exc:
        cli.resume_quarantine(db)
    assert exc.value.code == cli.EXIT_REFUSE
    assert "both" in exc.value.message


def test_other_names_are_not_partials(lab):
    (lab / "other.db.stale-20260101T000000.000001Z.partial").mkdir()
    (lab / "app.db.restore-20260101T000000.000001Z-abcd").mkdir()
    cli.resume_quarantine(lab / "app.db")
    assert len(list(lab.iterdir())) == 2


# --- 임시 복원·설치 ---------------------------------------------------------------------------


def ns(lab: Path, fake: Fake, **kw):
    args, _ = cli.parse_args(boot_args(lab, fake))
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def test_restore_to_temp_then_install(lab, fake):
    db = lab / "app.db"
    restored, txid = cli.restore_to_temp(db, ns(lab, fake))
    assert txid == 5
    assert restored.parent.parent == lab
    assert restored.parent.name.startswith("app.db.restore-")
    assert not db.exists()
    cli.install(restored, db)
    assert count_rows(db) == 7
    assert not restored.parent.exists()


def test_restore_failure_leaves_temp_dir(lab, fake):
    fake.fail_restore()
    with pytest.raises(cli._Exit) as exc:
        cli.restore_to_temp(lab / "app.db", ns(lab, fake))
    assert exc.value.code == cli.EXIT_RESTORE
    (tmpdir,) = lab.iterdir()
    assert tmpdir.name.startswith("app.db.restore-")
    assert str(tmpdir) in exc.value.message


def test_restore_with_unexpected_wal_is_failure(lab, fake):
    (fake.dir / "restore.extra").touch()
    with pytest.raises(cli._Exit) as exc:
        cli.restore_to_temp(lab / "app.db", ns(lab, fake))
    assert exc.value.code == cli.EXIT_RESTORE
    assert "app.db-wal" in exc.value.message


@pytest.mark.parametrize("existing", ["app.db", "app.db-wal"])
def test_install_refuses_when_target_exists(lab, fake, existing):
    restored, _ = cli.restore_to_temp(lab / "app.db", ns(lab, fake))
    (lab / existing).write_bytes(b"keep")
    with pytest.raises(cli._Exit) as exc:
        cli.install(restored, lab / "app.db")
    assert exc.value.code == cli.EXIT_REFUSE
    assert (lab / existing).read_bytes() == b"keep"
    assert restored.exists()


# --- 무결성 -------------------------------------------------------------------------------


def test_integrity_ok_and_read_only(lab):
    db = lab / "app.db"
    make_db(db, 2)
    before = db.read_bytes()
    assert cli.integrity_problem(db) is None
    assert db.read_bytes() == before


def test_integrity_garbage_file(lab):
    db = lab / "app.db"
    db.write_bytes(b"not a database" * 100)
    assert "cannot check" in cli.integrity_problem(db)


def test_integrity_corrupt_page(lab):
    db = lab / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("CREATE INDEX t_v ON t(v)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", [(f"{i:0100d}",) for i in range(2000)])
    conn.commit()
    conn.close()
    data = bytearray(db.read_bytes())
    page = 4096
    for i in range(page * 5 + 100, page * 5 + 400):
        data[i] ^= 0x5A
    db.write_bytes(bytes(data))
    assert cli.integrity_problem(db) is not None


def test_integrity_missing_db_does_not_create_it(lab):
    db = lab / "app.db"
    assert cli.integrity_problem(db) is not None
    assert not db.exists()


def test_integrity_path_with_special_characters(tmp_path):
    d = tmp_path / "a dir?#%"
    d.mkdir()
    make_db(d / "app.db", 1)
    assert cli.integrity_problem(d / "app.db") is None


# --- 상태 파일 ----------------------------------------------------------------------------


def test_state_file_is_written_atomically(lab):
    db = lab / "app.db"
    plan = cli._Plan("unknown", "keep_local", "remote_ahead", "local txid 1 < remote txid 2")
    path = cli.write_state(db, plan, litestream_version="0.5.17")
    assert path == lab / "app.db.boot-state.json"
    data = json.loads(path.read_text())
    assert data.pop("at").endswith("Z")
    assert data == {
        "version": 1,
        "state": "unknown",
        "action": "keep_local",
        "reason_code": "remote_ahead",
        "reason": "local txid 1 < remote txid 2",
        "unknown_at_boot": True,
        "litestream_version": "0.5.17",
    }
    assert [p.name for p in lab.iterdir()] == [path.name]


# --- 흐름 (가짜 litestream, in-process) ---------------------------------------------------------


def state(lab: Path) -> dict:
    return json.loads((lab / "app.db.boot-state.json").read_text())


def test_fresh_restores(lab, fake, no_exec):
    assert main_inproc(boot_args(lab, fake), no_exec) == "exec"
    assert count_rows(lab / "app.db") == 7
    assert state(lab)["action"] == "restore"
    assert no_exec[0] == [["true"]]


def test_new_db_requires_init_new(lab, fake, no_exec, capsys):
    fake.remote_txid(None)
    assert main_inproc(boot_args(lab, fake), no_exec) == cli.EXIT_REFUSE
    err = capsys.readouterr().err
    assert "no_replica_no_local" in err and "--init-new" in err
    assert main_inproc(boot_args(lab, fake, "--init-new"), no_exec) == "exec"
    assert not (lab / "app.db").exists()
    assert "turn off --init-new" in capsys.readouterr().err
    assert state(lab)["reason_code"] == "new_db"


def test_adopt_existing(lab, fake, no_exec, capsys):
    fake.remote_txid(None)
    make_db(lab / "app.db", 2)
    assert main_inproc(boot_args(lab, fake), no_exec) == cli.EXIT_REFUSE
    assert main_inproc(boot_args(lab, fake, "--adopt-existing"), no_exec) == "exec"
    assert "turn off --adopt-existing" in capsys.readouterr().err
    assert state(lab)["state"] == "adopt"


def test_match_proceeds_without_changes(lab, fake, no_exec):
    db, meta = lab / "app.db", None
    make_db(db, 4)
    meta = write_meta(db)
    fake.remote_txid(0x19)
    before = snapshot(lab)
    assert main_inproc(boot_args(lab, fake), no_exec) == "exec"
    after = snapshot(lab, skip=("app.db.boot-state.json", "app.db.boot.lock"))
    assert {k: v for k, v in after.items() if not k.startswith("app.db-")} == before
    assert meta.exists()
    assert state(lab)["state"] == "match"


def test_remote_error_refuses_even_with_restore_policy(lab, fake, no_exec, capsys):
    fake.remote_error()
    make_db(lab / "app.db", 1)
    write_meta(lab / "app.db")
    before = snapshot(lab)
    for policy in ("refuse", "restore", "keep-local"):
        assert main_inproc(boot_args(lab, fake, "--on-unknown", policy), no_exec) == 2
    assert "remote_error" in capsys.readouterr().err
    assert snapshot(lab, skip=("app.db.boot.lock",)) == before
    assert "restore" not in fake.calls()


def test_keep_local_records_unknown_at_boot(lab, fake, no_exec, capsys):
    make_db(lab / "app.db", 1)
    write_meta(lab / "app.db")
    fake.remote_txid(0x20)
    assert main_inproc(boot_args(lab, fake, "--on-unknown", "keep-local"), no_exec) == "exec"
    assert "WARNING: unknown_at_boot" in capsys.readouterr().err
    s = state(lab)
    assert (s["action"], s["unknown_at_boot"], s["reason_code"]) == (
        "keep_local",
        True,
        "remote_ahead",
    )


def test_quarantine_and_restore(lab, fake, no_exec):
    db, meta = local_volume(lab)
    fake.remote_txid(0x20)
    old = snapshot(lab)
    assert main_inproc(boot_args(lab, fake, "--on-unknown", "restore"), no_exec) == "exec"
    assert count_rows(db) == 7
    (final,) = stale_dirs(lab)
    assert snapshot(final, skip=("manifest.json",)) == old
    assert not meta.exists()
    assert state(lab)["action"] == "quarantine_and_restore"
    assert not [p for p in lab.iterdir() if ".restore-" in p.name]


def test_restore_failure_leaves_local_untouched(lab, fake, no_exec):
    local_volume(lab)
    fake.remote_txid(0x20)
    fake.fail_restore()
    before = snapshot(lab)
    assert main_inproc(boot_args(lab, fake, "--on-unknown", "restore"), no_exec) == 4
    after = snapshot(lab, skip=("app.db.boot.lock",))
    assert {k: v for k, v in after.items() if ".restore-" not in k} == before
    assert not stale_dirs(lab)
    assert not no_exec[0]


def test_integrity_failure_exits_3(lab, fake, no_exec):
    (lab / "app.db").write_bytes(b"garbage" * 1000)
    write_meta(lab / "app.db")
    fake.remote_txid(0x19)
    assert main_inproc(boot_args(lab, fake), no_exec) == cli.EXIT_INTEGRITY
    assert not (lab / "app.db.boot-state.json").exists()
    assert not no_exec[0]


@pytest.mark.parametrize("sidecar", ["app.db-wal", "app.db-shm", "app.db-journal"])
def test_orphan_sidecar_refuses(lab, fake, no_exec, capsys, sidecar):
    (lab / sidecar).write_bytes(b"x")
    assert main_inproc(boot_args(lab, fake), no_exec) == cli.EXIT_REFUSE
    assert "orphan_sidecars" in capsys.readouterr().err
    assert "restore" not in fake.calls()
    # restore 정책이어도 원격이 비었거나 실패면 거부한다.
    fake.remote_txid(None)
    assert main_inproc(boot_args(lab, fake, "--on-unknown", "restore"), no_exec) == 2
    fake.remote_error()
    assert main_inproc(boot_args(lab, fake, "--on-unknown", "restore"), no_exec) == 2
    assert (lab / sidecar).read_bytes() == b"x"


def test_orphan_sidecar_with_restore_policy_quarantines_and_restores(lab, fake, no_exec):
    (lab / "app.db-wal").write_bytes(b"x")
    assert main_inproc(boot_args(lab, fake, "--on-unknown", "restore"), no_exec) == "exec"
    (final,) = stale_dirs(lab)
    assert (final / "app.db-wal").read_bytes() == b"x"
    assert count_rows(lab / "app.db") == 7
    assert state(lab)["reason_code"] == "orphan_sidecars"


def test_restore_also_quarantines_unusable_meta(lab, fake, no_exec):
    meta = ls.default_meta_path(lab / "app.db")
    (meta / "ltx" / "0").mkdir(parents=True)  # 비어 있어 TXID 없음 → fresh/restore
    assert main_inproc(boot_args(lab, fake), no_exec) == "exec"
    (final,) = stale_dirs(lab)
    assert (final / ".app.db-litestream").is_dir()
    assert not meta.exists()


def test_db_path_that_is_not_a_regular_file_refuses(lab, fake, no_exec):
    (lab / "app.db").mkdir()
    assert main_inproc(boot_args(lab, fake), no_exec) == cli.EXIT_REFUSE
    assert fake.calls() == []


def test_interrupted_quarantine_is_finished_before_deciding(lab, fake, no_exec, monkeypatch):
    db, meta = local_volume(lab)
    with monkeypatch.context() as m:
        crash_on_rename(m, 4)
        with pytest.raises(KeyboardInterrupt):
            cli.quarantine(db, meta)
    # 남은 것: -shm, -journal, 메타. 재개 뒤 로컬이 비므로 fresh → 복원.
    assert main_inproc(boot_args(lab, fake), no_exec) == "exec"
    (final,) = stale_dirs(lab)
    assert len(os.listdir(final)) == 6
    assert count_rows(db) == 7
    assert state(lab)["action"] == "restore"


# --- 서브프로세스: exec 와 잠금 상속 ------------------------------------------------------------


def test_exec_replaces_process_and_inherits_lock(lab, fake):
    fake.remote_txid(None)
    db = lab / "app.db"
    cmd = marker_command(lab, db)
    result = run_boot(
        ["--db", str(db), "--config", "c.yml", "--litestream", str(fake.binary), "--init-new"]
        + ["--", *cmd, "hello", "world"]
    )
    assert result.returncode == 0, result.stderr
    assert read_marker(lab) == {"argv": ["hello", "world"], "lock_held": True}
    assert "[boot] exec:" in result.stderr
    assert not db.exists()


def test_exec_failure_exits_127(lab, fake):
    fake.remote_txid(None)
    result = run_boot(
        ["--db", str(lab / "app.db"), "--config", "c.yml", "--litestream", str(fake.binary)]
        + ["--init-new", "--", str(lab / "no-such-command")]
    )
    assert result.returncode == 127
    assert "cannot exec" in result.stderr


def test_stderr_tells_the_story(lab, fake):
    result = run_boot(
        ["--db", str(lab / "app.db"), "--config", "c.yml", "--litestream", str(fake.binary)]
        + ["--", "true"]
    )
    assert result.returncode == 0, result.stderr
    lines = result.stderr.splitlines()
    assert all(line.startswith("[boot] ") for line in lines), lines
    text = result.stderr
    for needle in (
        "lock:",
        "inputs: local_exists=False local_txid=None remote=txid 5",
        "decision: state=fresh action=restore reason_code=restore_from_remote",
        "restore: ok, txid 5",
        "install:",
        "integrity: quick_check ok",
        "state:",
        "exec: true",
    ):
        assert needle in text, needle


# --- Django 차단 --------------------------------------------------------------------------


def test_boot_main_imports_without_django():
    assert_runs_without_django(
        """
        import django_sqlite_ops.boot.__main__
        import django_sqlite_ops.boot.cli as cli

        assert cli.EXIT_REFUSE == 2
        """
    )


# --- L 시나리오: 실제 litestream -------------------------------------------------------------


def _replicate_until_caught_up(db: Path, config: Path, *, above: int = 0) -> int:
    """replicate 를 돌려 원격과 로컬 메타의 TXID 가 ``above`` 보다 커진 뒤 같아질 때까지 기다린다."""
    proc = subprocess.Popen(
        [LITESTREAM, "replicate", "-config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
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


def _insert(db: Path, start: int, n: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(start, start + n):
        conn.execute("INSERT INTO t(v) VALUES (?)", (f"row-{i}" * 50,))
        conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def seed(tmp_path_factory) -> Path:
    """복제본과 두 볼륨: ``old``(10행까지 복제) · ``vol``(20행까지 복제). 테스트마다 복사해 쓴다."""
    require_litestream()
    root = tmp_path_factory.mktemp("seed").resolve()
    data = root / "data"
    data.mkdir()
    db = data / "app.db"
    config = write_config(root, db, f"      type: file\n      path: {root / 'replica'}\n")
    _insert(db, 0, 10)
    first = _replicate_until_caught_up(db, config)
    shutil.copytree(data, root / "old")
    _insert(db, 10, 10)
    _replicate_until_caught_up(db, config, above=first)
    shutil.copytree(data, root / "vol")
    assert count_rows(root / "old" / "app.db") == 10
    assert count_rows(root / "vol" / "app.db") == 20
    return root


def real_lab(tmp_path: Path, seed: Path, volume: str | None) -> tuple[Path, Path, Path]:
    """(data 디렉터리, db, config). ``volume`` 이 None 이면 빈 볼륨(새 컨테이너)."""
    root = tmp_path.resolve()
    shutil.copytree(seed / "replica", root / "replica")
    data = root / "data"
    if volume is None:
        data.mkdir()
    else:
        shutil.copytree(seed / volume, data)
    db = data / "app.db"
    config = write_config(root, db, f"      type: file\n      path: {root / 'replica'}\n")
    return data, db, config


def real_args(db: Path, config: Path, *extra: str, cmd: list[str]) -> list[str]:
    return [
        "--db",
        str(db),
        "--config",
        str(config),
        "--litestream",
        LITESTREAM,
        *extra,
        "--",
        *cmd,
    ]


@pytest.fixture
def real(litestream_binary, seed, tmp_path):
    def make(volume: str | None):
        return real_lab(tmp_path, seed, volume)

    return make


def test_l1_fresh_container_restores(real):
    data, db, config = real(None)
    result = run_boot(real_args(db, config, cmd=marker_command(data, db)))
    assert result.returncode == 0, result.stderr
    assert read_marker(data)["lock_held"] is True
    assert count_rows(db) == 20
    s = state(data)
    assert (s["state"], s["action"], s["litestream_version"]) == ("fresh", "restore", "0.5.17")


def test_l2_old_volume_refuses_and_replica_untouched(real):
    data, db, config = real("old")
    replica = config.parent / "replica"
    before_replica, before_data = snapshot(replica), snapshot(data)
    result = run_boot(real_args(db, config, cmd=marker_command(data, db)))
    assert result.returncode == 2, result.stderr
    assert "reason_code=remote_ahead" in result.stderr
    assert "--on-unknown restore" in result.stderr
    assert read_marker(data) is None
    assert snapshot(replica) == before_replica
    assert snapshot(data, skip=("app.db.boot.lock",)) == before_data


def test_l3_local_meta_deleted_refuses(real):
    data, db, config = real("vol")
    shutil.rmtree(data / ".app.db-litestream")
    result = run_boot(real_args(db, config, cmd=marker_command(data, db)))
    assert result.returncode == 2, result.stderr
    assert "reason_code=no_local_meta" in result.stderr
    assert "D-14" in result.stderr
    assert count_rows(db) == 20


def test_l4_unresponsive_remote_refuses_within_timeout(real):
    data, db, _ = real("vol")
    root = data.parent
    with SilentServer() as server:
        config = write_config(
            root,
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
        result = run_boot(
            real_args(db, config, "--ltx-timeout", "3", cmd=marker_command(data, db)),
            env={"AWS_EC2_METADATA_DISABLED": "true"},
        )
        elapsed = time.monotonic() - start
        assert server.conns, "litestream never connected"
    assert result.returncode == 2, result.stderr
    assert "reason_code=remote_error" in result.stderr
    assert "timed out after 3s" in result.stderr
    assert elapsed < 3 + 15
    assert read_marker(data) is None


def test_l5_unreplicated_local_commit_is_kept(real):
    data, db, config = real("vol")
    _insert(db, 20, 1)
    result = run_boot(real_args(db, config, cmd=marker_command(data, db)))
    assert result.returncode == 0, result.stderr
    assert "state=match" in result.stderr
    assert count_rows(db) == 21
    assert read_marker(data) is not None


KILL_WRAPPER = """
import os, runpy, sys
kill_at = int(os.environ["KILL_AT"])
log = os.environ["RENAME_LOG"]
real_rename, real_exec = os.rename, os.execvp
count = 0

def rename(src, dst, *a, **kw):
    global count
    count += 1
    with open(log, "a") as f:
        f.write(f"{count} {src} -> {dst}\\n")
    if count == kill_at:
        os._exit(99)
    return real_rename(src, dst, *a, **kw)

def execvp(file, args):
    if kill_at == -1:
        os._exit(98)
    return real_exec(file, args)

os.rename, os.execvp = rename, execvp
sys.argv[0] = "django_sqlite_ops.boot"
runpy.run_module("django_sqlite_ops.boot", run_name="__main__", alter_sys=True)
"""


def run_killed(args: list[str], kill_at: int, log: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", KILL_WRAPPER, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "KILL_AT": str(kill_at), "RENAME_LOG": str(log)},
        check=False,
    )


def _count_renames(real) -> list[str]:
    data, db, config = real("old")
    log = data.parent / "renames.log"
    result = run_killed(real_args(db, config, "--on-unknown", "restore", cmd=["true"]), 0, log)
    assert result.returncode == 0, result.stderr
    return log.read_text().splitlines()


def test_l6_rename_sequence(real):
    renames = [line.split(" ", 1)[1] for line in _count_renames(real)]
    kinds = [r.split(" -> ")[0].rsplit("/", 1)[1] for r in renames]
    # manifest, 볼륨 파일(있는 것만, 순서대로), partial→final, 설치, 상태 파일
    assert kinds[0].startswith("manifest.json.tmp-")
    assert kinds[-3].endswith(".partial")
    assert kinds[-2] == "app.db"
    assert kinds[-1].startswith("app.db.boot-state.json.tmp-")
    moved = kinds[1:-3]
    present = [
        n for n in ("app.db", "app.db-wal", "app.db-shm", ".app.db-litestream") if n in moved
    ]
    assert moved == present
    assert moved[0] == "app.db" and moved[-1] == ".app.db-litestream"


# kill 지점: k 번째 rename 직전(1..8), 그리고 exec 직전(-1).
# 옛 볼륨(db·-wal·-shm·메타)에서 rename 은 8번이다: manifest, app.db, -wal, -shm, 메타,
# partial→final, 설치, 상태 파일(test_l6_rename_sequence). 볼륨에 -wal 이 없으면 줄어들어 skip 된다.
L6_MAX_RENAMES = 8


@pytest.mark.parametrize("kill_at", [*range(1, L6_MAX_RENAMES + 1), -1])
def test_l6_kill_during_quarantine_then_rerun_completes(real, kill_at):
    data, db, config = real("old")
    old_db = hashlib.sha256((data / "app.db").read_bytes()).hexdigest()
    log = data.parent / "renames.log"
    args = real_args(db, config, "--on-unknown", "restore", cmd=marker_command(data, db))
    killed = run_killed(args, kill_at, log)
    total = len(log.read_text().splitlines()) if log.exists() else 0
    if kill_at > 0 and killed.returncode == 0:
        pytest.skip(f"only {total} renames in this run")
    assert killed.returncode in (98, 99), killed.stderr
    assert read_marker(data) is None

    rerun = run_boot(args)
    assert rerun.returncode == 0, rerun.stderr
    assert read_marker(data) is not None
    assert count_rows(db) == 20
    assert not [p for p in data.iterdir() if p.name.endswith(".partial")]
    assert not ls.default_meta_path(db).exists()

    # 옛 볼륨은 통째로 한 격리 디렉터리에 있다(DB 와 메타가 갈라지지 않는다).
    homes = [d for d in stale_dirs(data) if (d / "app.db").exists()]
    old_homes = [
        d for d in homes if hashlib.sha256((d / "app.db").read_bytes()).hexdigest() == old_db
    ]
    assert len(old_homes) == 1, [sorted(os.listdir(d)) for d in stale_dirs(data)]
    assert (old_homes[0] / ".app.db-litestream").is_dir()
    assert count_rows(old_homes[0] / "app.db") == 10
    for d in stale_dirs(data):
        assert set(os.listdir(d)) <= {"manifest.json", *VOLUME_NAMES}


def test_l7_corrupt_replica_restore_fails_local_unchanged(real):
    data, db, config = real("old")
    for ltx in (config.parent / "replica").rglob("*.ltx"):
        raw = bytearray(ltx.read_bytes())
        for i in range(100, len(raw)):
            raw[i] ^= 0xFF
        ltx.write_bytes(bytes(raw))
    before = snapshot(data)
    result = run_boot(
        real_args(db, config, "--on-unknown", "restore", cmd=marker_command(data, db))
    )
    assert result.returncode == 4, result.stderr
    assert "restore failed" in result.stderr
    after = snapshot(data, skip=("app.db.boot.lock",))
    assert {k: v for k, v in after.items() if ".restore-" not in k} == before
    assert not stale_dirs(data)
    assert read_marker(data) is None


def test_first_deploy_needs_init_new(litestream_binary, tmp_path):
    root = tmp_path.resolve()
    data = root / "data"
    data.mkdir()
    db = data / "app.db"
    config = write_config(root, db, f"      type: file\n      path: {root / 'replica'}\n")
    result = run_boot(real_args(db, config, cmd=marker_command(data, db)))
    assert result.returncode == 2, result.stderr
    assert "no_replica_no_local" in result.stderr
    result = run_boot(real_args(db, config, "--init-new", cmd=marker_command(data, db)))
    assert result.returncode == 0, result.stderr
    assert read_marker(data) is not None
    assert not db.exists()
    assert "turn off --init-new" in result.stderr


def test_lock_held_by_execd_command_blocks_second_boot(real):
    data, db, config = real("vol")
    first = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "django_sqlite_ops.boot",
            *real_args(db, config, cmd=[*marker_command(data, db), "--sleep"]),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while read_marker(data) is None:
            assert first.poll() is None, first.stderr.read()
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert read_marker(data)["lock_held"] is True
        second = run_boot(real_args(db, config, cmd=["true"]))
        assert second.returncode == 5, second.stderr
        assert "held by another boot" in second.stderr
    finally:
        first.kill()
        first.wait(timeout=10)
    third = run_boot(real_args(db, config, cmd=["true"]))
    assert third.returncode == 0, third.stderr
