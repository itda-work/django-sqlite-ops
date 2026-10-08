"""``manage.py sqlite_doctor`` (DESIGN §6-2).

Django 설정은 프로세스당 한 번만 정할 수 있어서, 시나리오마다 새 인터프리터에서
``settings.configure()`` → ``django.setup()`` 을 하고 명령을 실행한다
(``test_checks.py`` 와 같은 방식).
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from _litestream import needs_litestream

from django_sqlite_ops.boot.litestream import parse_databases_json
from django_sqlite_ops.database import sqlite_database
from django_sqlite_ops.doctor import (
    Item,
    classify_fstype,
    exit_code,
    filesystem_type,
    format_text,
    mount_output_fstype,
    mountinfo_fstype,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "litestream-0.5.17"

RUN_DOCTOR_SCRIPT = """
import json
import sys

import django
from django.conf import settings

config = json.loads(sys.argv[1])
settings.configure(INSTALLED_APPS=["django_sqlite_ops"], USE_TZ=True, **config)
django.setup()

from django.core.management import execute_from_command_line

execute_from_command_line(["manage.py", "sqlite_doctor", *sys.argv[2:]])
"""


def doctor(databases, *args, json_output=True, **extra):
    config = {"DATABASES": databases, **extra}
    argv = [sys.executable, "-c", RUN_DOCTOR_SCRIPT, json.dumps(config), *args]
    if json_output:
        argv.append("--json")
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode in (0, 1, 2), result.stderr
    if not json_output:
        return result.returncode, result.stdout
    data = json.loads(result.stdout)
    assert data["exit_code"] == result.returncode
    return result.returncode, data


def items(data, section=None, alias=None, key=None):
    return [
        i
        for i in data["items"]
        if (section is None or i["section"] == section)
        and (alias is None or i["alias"] == alias)
        and (key is None or i["key"] == key)
    ]


def one(data, section, alias, key):
    found = items(data, section, alias, key)
    assert len(found) == 1, found
    return found[0]


def problems(data, *, ignore_sections=("mount",)):
    return [i for i in data["items"] if i["level"] != "ok" and i["section"] not in ignore_sections]


def make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    return path


# Django 는 "default" 별칭을 요구한다. 다른 별칭만 보는 테스트에 넣는다.
MEMORY = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}


def raw(name, **options):
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(name), "OPTIONS": options}


# --- DB 별칭 ---------------------------------------------------------------------------


def test_recommended_settings_have_no_warnings(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor({"default": sqlite_database(db)})
    assert problems(data) == []
    assert one(data, "database", "default", "journal_mode")["value"] == "WAL"
    assert one(data, "database", "default", "synchronous")["value"] == "NORMAL"
    assert one(data, "database", "default", "busy_timeout")["value"] == 5000
    assert one(data, "database", "default", "transaction_mode")["value"] == "IMMEDIATE"
    assert one(data, "database", "default", "foreign_keys")["value"] == 1
    assert one(data, "database", "default", "sqlite_version")["value"] == sqlite3.sqlite_version
    assert one(data, "database", "default", "file")["value"] == db.stat().st_size
    # 로컬 디스크이므로 마운트도 문제 없음
    assert one(data, "mount", "default", "filesystem")["level"] == "ok"
    assert rc == 0


def test_missing_file_is_error_and_not_created(tmp_path):
    db = tmp_path / "missing.sqlite3"
    rc, data = doctor({"default": sqlite_database(db)})
    item = one(data, "database", "default", "file")
    assert item["level"] == "error"
    assert "not connected" in item["message"]
    assert not db.exists()
    assert not Path(f"{db}-wal").exists()
    assert sorted(os.listdir(tmp_path)) == []
    assert items(data, "database", "default", "journal_mode") == []
    assert rc == 2


def test_missing_uri_file_is_not_created(tmp_path):
    db = tmp_path / "missing.sqlite3"
    rc, data = doctor({"default": raw(f"file:{db}?mode=rwc")})
    assert one(data, "database", "default", "file")["level"] == "error"
    assert not db.exists()
    assert rc == 2


def test_values_differing_from_recommended_warn(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    config = sqlite_database(db, pragmas={"synchronous": "FULL", "busy_timeout": 100})
    rc, data = doctor({"default": config})
    warned = {i["key"]: i for i in problems(data)}
    assert set(warned) == {"synchronous", "busy_timeout"}
    assert warned["synchronous"]["value"] == "FULL"
    assert warned["synchronous"]["expected"] == "NORMAL"
    assert warned["busy_timeout"]["value"] == 100
    assert warned["busy_timeout"]["expected"] == 5000
    assert rc == 1


def test_plain_settings_warn_on_actual_values(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor({"default": raw(db)})
    warned = {i["key"]: i["value"] for i in problems(data)}
    # Python sqlite3 기본 timeout 5초 = busy_timeout 5000 이라 그 항목은 맞는다
    assert warned == {"transaction_mode": None, "journal_mode": "DELETE", "synchronous": "FULL"}
    assert rc == 1


def test_init_command_effect_is_what_doctor_sees(tmp_path):
    # 정적 체크는 설정 문자열을 보지만 doctor 는 init_command 가 실행된 뒤의 실제 값을 본다
    db = make_db(tmp_path / "app.sqlite3")
    config = raw(db, transaction_mode="IMMEDIATE", init_command="PRAGMA synchronous=1")
    rc, data = doctor({"default": config})
    assert one(data, "database", "default", "synchronous")["value"] == "NORMAL"
    assert one(data, "database", "default", "journal_mode")["level"] == "warn"


def test_read_only_alias_skips_write_recommendations(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {
            "default": MEMORY,
            "ro": raw(f"file:{db}?mode=ro", init_command="PRAGMA busy_timeout=5000"),
        }
    )
    assert one(data, "database", "ro", "role")["value"] == "read-only"
    assert one(data, "database", "ro", "journal_mode")["level"] == "ok"
    assert one(data, "database", "ro", "journal_mode")["expected"] is None
    assert one(data, "database", "ro", "busy_timeout")["expected"] == 5000
    assert problems(data) == []
    assert rc == 0


def test_memory_alias_is_reported_as_memory():
    rc, data = doctor({"default": raw(":memory:")})
    assert [(i["key"], i["level"], i["value"]) for i in items(data, "database")] == [
        ("role", "ok", "memory")
    ]
    assert items(data, "mount") == []
    assert rc == 0


@pytest.mark.parametrize(
    "name",
    [
        "file:/nonexistent/app.sqlite3?vfs=litestream&mode=ro",
        "file:/nonexistent/app.sqlite3?mode=ro&mode=rwc",
    ],
)
def test_vfs_and_unknown_roles_are_not_connected(name):
    rc, data = doctor({"default": raw(name)})
    item = one(data, "database", "default", "role")
    assert item["level"] == "unknown"
    assert "not connected" in item["message"]
    assert rc == 1


def test_connection_failure_is_error(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    # 읽기 전용 연결에 WAL 을 쓰려 하면 연결이 깨진다(DESIGN §6-1)
    rc, data = doctor(
        {"default": MEMORY, "ro": raw(f"file:{db}?mode=ro", init_command="PRAGMA journal_mode=WAL")}
    )
    item = one(data, "database", "ro", "connect")
    assert item["level"] == "error"
    assert "\n" not in item["message"]
    assert rc == 2


def test_database_option_selects_aliases(tmp_path):
    a = make_db(tmp_path / "a.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(a), "other": sqlite_database(tmp_path / "b.sqlite3")},
        "--database",
        "default",
    )
    assert {i["alias"] for i in items(data, "database")} == {"default"}
    assert rc == 0
    rc, data = doctor({"default": sqlite_database(a)}, "--database", "nope")
    assert one(data, "database", "nope", "alias")["level"] == "error"
    assert rc == 2


def test_profile_error_is_reported(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor({"default": sqlite_database(db)}, SQLITE_OPS={"PROFILE": "nope"})
    assert one(data, "settings", None, "profile")["level"] == "error"
    assert rc == 2


# --- 출력 ------------------------------------------------------------------------------


def test_json_schema(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor({"default": sqlite_database(db)})
    assert data["version"] == 1
    assert set(data) == {"version", "items", "summary", "exit_code"}
    assert set(data["summary"]) == {"ok", "warn", "error", "unknown"}
    for item in data["items"]:
        assert set(item) == {"section", "alias", "key", "level", "value", "expected", "message"}
        assert item["level"] in ("ok", "warn", "error", "unknown")
        assert item["section"] in ("settings", "database", "mount", "litestream", "channels")


def test_text_output(tmp_path):
    db = make_db(tmp_path / "app.sqlite3")
    rc, out = doctor(
        {"default": sqlite_database(db, pragmas={"synchronous": "FULL"})}, json_output=False
    )
    assert "[database]" in out
    assert any(line.split()[:3] == ["WARN", "default", "synchronous"] for line in out.splitlines())
    assert out.rstrip().splitlines()[-1].endswith("-> exit 1")
    assert rc == 1


@pytest.mark.parametrize(
    ("levels", "code"),
    [
        ([], 0),
        (["ok"], 0),
        (["ok", "unknown"], 1),
        (["warn"], 1),
        (["warn", "error"], 2),
        (["unknown", "error"], 2),
    ],
)
def test_exit_code(levels, code):
    assert exit_code([Item("database", None, "k", level) for level in levels]) == code


def test_format_text_summary_counts():
    text = format_text(
        [
            Item("database", "a", "x", "warn", 1, 2),
            Item("mount", "a", "filesystem", "unknown"),
            Item("litestream", None, "config", "error", message="boom"),
        ]
    )
    assert text.splitlines()[-1] == "summary: 1 warning(s), 1 error(s), 1 unknown -> exit 2"


# --- 마운트 ----------------------------------------------------------------------------

MOUNTINFO = r"""22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
30 22 0:40 / /srv/nfs rw,relatime shared:2 - nfs server:/export rw,vers=3
31 22 0:41 / /srv/nfs4 rw shared:3 - nfs4 server:/export rw
32 22 0:42 / /srv/cifs rw - cifs //server/share rw
33 22 0:43 / /srv/smb3 rw - smb3 //server/share rw
34 22 0:44 / /srv/sshfs rw - fuse.sshfs user@host:/ rw
35 22 0:45 / /var/lib/docker/overlay rw - overlay overlay rw
36 30 8:2 / /srv/nfs/local rw - ext4 /dev/sdb1 rw
37 22 0:46 / /srv/with\040space rw - nfs server:/x rw
38 22 0:47 / /srv/fuse rw - fuse.mystery x rw
39 22 0:48 / /srv/overmount rw - ext4 /dev/sdc1 rw
40 39 0:49 / /srv/overmount rw - nfs server:/y rw
"""


@pytest.mark.parametrize(
    ("path", "fstype", "level"),
    [
        ("/srv/app/app.sqlite3", "ext4", "ok"),
        ("/srv/nfs/app.sqlite3", "nfs", "warn"),
        ("/srv/nfs4/app.sqlite3", "nfs4", "warn"),
        ("/srv/cifs/app.sqlite3", "cifs", "warn"),
        ("/srv/smb3/app.sqlite3", "smb3", "warn"),
        ("/srv/sshfs/app.sqlite3", "fuse.sshfs", "warn"),
        ("/var/lib/docker/overlay/app.sqlite3", "overlay", "ok"),
        ("/srv/nfs/local/app.sqlite3", "ext4", "ok"),  # 가장 긴 마운트 지점
        ("/srv/nfsx/app.sqlite3", "ext4", "ok"),  # 구성요소 단위 접두사
        ("/srv/with space/app.sqlite3", "nfs", "warn"),
        ("/srv/fuse/app.sqlite3", "fuse.mystery", "unknown"),
        ("/srv/overmount/app.sqlite3", "nfs", "warn"),  # 같은 지점이면 나중 줄
    ],
)
def test_mountinfo_table(path, fstype, level):
    found = mountinfo_fstype(MOUNTINFO, path)
    assert found == fstype
    assert classify_fstype(found)[0] == level


MOUNT_OUTPUT = """/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
//user@nas/home on /Volumes/home (afpfs, nodev, nosuid, mounted by user)
//user@nas/share on /Volumes/share (smbfs, nodev, nosuid, mounted by user)
nas:/export on /Volumes/nfs (nfs, nodev, nosuid)
"""


@pytest.mark.parametrize(
    ("path", "fstype", "level"),
    [
        ("/System/Volumes/Data/app.sqlite3", "apfs", "ok"),
        ("/usr/app.sqlite3", "apfs", "ok"),
        ("/Volumes/home/app.sqlite3", "afpfs", "warn"),
        ("/Volumes/share/app.sqlite3", "smbfs", "warn"),
        ("/Volumes/nfs/app.sqlite3", "nfs", "warn"),
    ],
)
def test_mount_output_table(path, fstype, level):
    found = mount_output_fstype(MOUNT_OUTPUT, path)
    assert found == fstype
    assert classify_fstype(found)[0] == level


def test_unknown_fstype():
    assert classify_fstype(None)[0] == "unknown"
    assert classify_fstype("")[0] == "unknown"
    assert mountinfo_fstype("garbage\n", "/x") is None


def test_real_local_filesystem_is_not_network(tmp_path):
    fstype = filesystem_type(str(tmp_path / "not-yet.sqlite3"))
    assert fstype is not None
    assert classify_fstype(fstype)[0] != "warn", fstype


# --- Litestream ------------------------------------------------------------------------


def _fixture(name):
    return (FIXTURES / name / "stdout").read_text(), int((FIXTURES / name / "rc").read_text())


def test_parse_databases_fixture_paths():
    # 저장된 fixture 는 랩 경로를 '$LAB' 으로 바꿨으므로 절대 경로로 되돌려 파싱한다
    stdout, _ = _fixture("databases_json")
    paths = parse_databases_json(stdout.replace("$LAB", "/lab"))
    assert paths == [
        "/lab/app.db",
        "/lab/link/linked.db",
        "/lab/dotdot.db",
        "/lab/cwd/relative.db",
        "/lab/cwd",
    ]
    stdout, rc = _fixture("databases_empty")
    assert (rc, parse_databases_json(stdout)) == (0, [])


@pytest.mark.parametrize(
    "stdout",
    ["", "{}", "[1]", '[{"path": 1}]', '[{"path": "relative.db"}]', "not json"],
)
def test_parse_databases_rejects_unexpected(stdout):
    assert isinstance(parse_databases_json(stdout), str)


def _config(lab: Path, *paths: str) -> Path:
    lines = ["dbs:"]
    for i, path in enumerate(paths):
        lines += [
            f"  - path: {path}",
            "    replica:",
            "      type: file",
            f"      path: {lab}/r{i}",
        ]
    config = lab / "litestream.yml"
    config.write_text("\n".join(lines) + "\n")
    return config


@needs_litestream
def test_litestream_config_matches(tmp_path):
    lab = Path(os.path.realpath(tmp_path))
    app = make_db(lab / "app.sqlite3")
    other = make_db(lab / "other.sqlite3")
    (lab / "real").mkdir()
    (lab / "link").symlink_to(lab / "real")
    config = _config(lab, str(app), f"{lab}/extra.db", f"{lab}/link/x.db", "relative.db")
    rc, data = doctor(
        {"default": sqlite_database(app), "other": sqlite_database(other)},
        "--litestream-config",
        str(config),
    )
    assert one(data, "litestream", None, "config")["level"] == "ok"
    assert one(data, "litestream", "default", "replicated")["level"] == "ok"
    assert one(data, "litestream", "other", "replicated")["level"] == "warn"
    extra = {i["value"] for i in items(data, "litestream", None, "extra")}
    assert f"{lab}/extra.db" in extra
    config_paths = {i["value"]: i for i in items(data, "litestream", None, "config_path")}
    assert config_paths[f"{lab}/link/x.db"]["level"] == "warn"
    assert "D-15" in config_paths[f"{lab}/link/x.db"]["message"]
    relative = [p for p in config_paths if p.endswith("/relative.db")]
    assert len(relative) == 1 and "relative" in config_paths[relative[0]]["message"]
    assert rc == 1


@needs_litestream
def test_litestream_config_via_link_path_matches_by_real_path(tmp_path):
    # DATABASES 가 링크 경로를 써도 실제 경로로 대조한다
    lab = Path(os.path.realpath(tmp_path))
    (lab / "real").mkdir()
    (lab / "link").symlink_to(lab / "real")
    app = make_db(lab / "real" / "app.sqlite3")
    config = _config(lab, str(app))
    rc, data = doctor(
        {"default": sqlite_database(lab / "link" / "app.sqlite3")},
        "--litestream-config",
        str(config),
    )
    assert one(data, "litestream", "default", "replicated")["level"] == "ok"
    assert rc == 0


@needs_litestream
def test_litestream_config_missing_is_error(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app)}, "--litestream-config", str(tmp_path / "nope.yml")
    )
    item = one(data, "litestream", None, "config")
    assert item["level"] == "error"
    assert "config file not found" in item["message"]
    assert rc == 2


def test_litestream_skipped_without_config(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor({"default": sqlite_database(app)})
    item = one(data, "litestream", None, "config")
    assert (item["level"], item["message"].split(":")[0]) == ("ok", "skipped")


def test_litestream_binary_missing_is_error(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app)},
        "--litestream-config",
        str(tmp_path / "ls.yml"),
        "--litestream",
        str(tmp_path / "no-such-litestream"),
    )
    assert one(data, "litestream", None, "config")["level"] == "error"
    assert rc == 2


# --- 채널 레이어 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "level", "word"),
    [
        ("channels.layers.InMemoryChannelLayer", "ok", "one process"),
        ("channels_nats.NatsChannelLayer", "ok", "dropped"),
        ("channels_redis.core.RedisChannelLayer", "ok", "kept until expiry"),
        ("channels_redis.pubsub.RedisPubSubChannelLayer", "unknown", "not measured"),
        ("myproject.layers.Custom", "unknown", "unknown"),
    ],
)
def test_channel_backends(tmp_path, backend, level, word):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app)}, CHANNEL_LAYERS={"default": {"BACKEND": backend}}
    )
    item = one(data, "channels", "default", "backend")
    assert (item["level"], item["value"]) == (level, backend)
    assert word in item["message"]


def test_channel_layers_not_set(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor({"default": sqlite_database(app)})
    assert one(data, "channels", None, "backend")["level"] == "ok"
    assert rc == 0


def test_inmemory_warns_under_multiproc_profile(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app, profile="single-server-multiproc")},
        SQLITE_OPS={"PROFILE": "single-server-multiproc"},
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    )
    assert one(data, "channels", "default", "backend")["level"] == "warn"
    assert rc == 1


LITE = "channels_lite.layers.aio.AIOSQLiteChannelLayer"


def test_channels_lite_same_file_as_app_warns(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app)},
        CHANNEL_LAYERS={"default": {"BACKEND": LITE, "CONFIG": {"database": "default"}}},
    )
    assert one(data, "channels", "default", "database")["level"] == "warn"
    assert rc == 1


def test_channels_lite_alias_sharing_file_warns(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app), "channels": sqlite_database(app)},
        CHANNEL_LAYERS={"default": {"BACKEND": LITE, "CONFIG": {"database": "channels"}}},
    )
    item = one(data, "channels", "default", "database")
    assert item["level"] == "warn" and "default" in item["message"]


def test_channels_lite_separate_file_ok(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    chan = make_db(tmp_path / "channels.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app), "channels": sqlite_database(chan)},
        CHANNEL_LAYERS={"default": {"BACKEND": LITE, "CONFIG": {"database": "channels"}}},
    )
    assert one(data, "channels", "default", "database")["level"] == "ok"
    # Litestream 설정이 없으면 복제 대상 규칙은 보지 않는다
    assert items(data, "channels", "default", "replicated") == []
    assert rc == 0


def test_channels_lite_missing_database_is_unknown(tmp_path):
    app = make_db(tmp_path / "app.sqlite3")
    rc, data = doctor(
        {"default": sqlite_database(app)},
        CHANNEL_LAYERS={"default": {"BACKEND": LITE}},
    )
    assert one(data, "channels", "default", "database")["level"] == "unknown"


@needs_litestream
def test_channels_lite_replicated_warns(tmp_path):
    lab = Path(os.path.realpath(tmp_path))
    app = make_db(lab / "app.sqlite3")
    chan = make_db(lab / "channels.sqlite3")
    layers = {"default": {"BACKEND": LITE, "CONFIG": {"database": "channels"}}}
    databases = {"default": sqlite_database(app), "channels": sqlite_database(chan)}
    rc, data = doctor(
        databases,
        "--litestream-config",
        str(_config(lab, str(app), str(chan))),
        CHANNEL_LAYERS=layers,
    )
    assert one(data, "channels", "default", "replicated")["level"] == "warn"
    rc, data = doctor(
        databases, "--litestream-config", str(_config(lab, str(app))), CHANNEL_LAYERS=layers
    )
    assert one(data, "channels", "default", "replicated")["level"] == "ok"
    # 복제하지 않는 채널 DB 는 쓰기 별칭이라도 "복제되지 않음" 경고가 나온다
    assert one(data, "litestream", "channels", "replicated")["level"] == "warn"


DOCTOR_NO_CHANNELS_SCRIPT = """
import json
import sys

import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=["django_sqlite_ops"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    CHANNEL_LAYERS={"default": {"BACKEND": "channels_redis.core.RedisChannelLayer"}},
)
django.setup()
from django_sqlite_ops.doctor import diagnose

diagnose()
leaked = sorted(m for m in sys.modules if m.split(".")[0] in ("channels", "channels_redis"))
print(json.dumps(leaked))
"""


def test_channels_not_imported():
    result = subprocess.run(
        [sys.executable, "-c", DOCTOR_NO_CHANNELS_SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []
