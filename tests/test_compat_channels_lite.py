"""channels-lite aio 중복 배달 호환 패치 (DESIGN §9).

결함: ``AIOSQLiteChannelLayer._receive_single_from_db`` 는 ``conn.total_changes > 0`` 으로
선점 성공을 판정한다. 풀에서 재사용된 연결이 앞서 쓰기를 했으면 경쟁에서 진 수신자도
같은 메시지를 받는다.

재현 시나리오는 새 인터프리터에서 돈다(Django 설정은 프로세스당 한 번, 패치는 클래스를 바꾼다).
channels-lite[aio] 가 없으면 건너뛴다.
"""

import importlib.util
import json
import subprocess
import sys
import textwrap
from difflib import unified_diff
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

needs_channels_lite_aio = pytest.mark.skipif(
    any(
        importlib.util.find_spec(m) is None for m in ("channels_lite", "aiosqlite", "aiosqlitepool")
    ),
    reason="channels-lite[aio] is not installed",
)

# 수신자 둘(레이어 인스턴스 둘 = 프로세스 둘)이 같은 일반 채널 하나를 경쟁한다. 송신자는 따로다.
# - prior: 수신자마다 다른 채널로 먼저 send 한다. 풀이 그 연결을 다시 주므로 total_changes > 0.
# - barrier: 두 수신자가 SELECT 를 마친 뒤에야 UPDATE 로 넘어간다(대상 메서드는 그대로 두고
#   연결 프록시에서 기다린다). 그래서 두 수신자가 늘 같은 행을 두고 경쟁하며, 결과가 결정적이다.
# - natural: 계측 없이 공개 API receive() 두 개를 동시에 돌린다(기본 pool_size).
REPRO_SCRIPT = """
import asyncio
import contextlib
import json
import os
import sys

import django
from django.conf import settings

directory, patch, prior, mode, rounds = sys.argv[1:6]
prior, rounds = prior == "1", int(rounds)
settings.configure(
    INSTALLED_APPS=["channels_lite", "django_sqlite_ops"],
    USE_TZ=True,
    DATABASES={
        alias: {"ENGINE": "django.db.backends.sqlite3", "NAME": os.path.join(directory, name)}
        for alias, name in (("default", "app.db"), ("channels", "ch.db"))
    },
    SQLITE_OPS={"PATCH_CHANNELS_LITE_AIO": patch == "setting"},
)
django.setup()
from django.core.management import call_command

call_command("migrate", database="channels", verbosity=0)

from channels_lite.layers import ChannelEmpty
from channels_lite.layers.aio import AIOSQLiteChannelLayer

from django_sqlite_ops.compat import channels_lite

if patch == "apply":
    assert channels_lite.apply()
assert channels_lite.status().applied == (patch != "none"), channels_lite.status()


class GatedConnection:
    def __init__(self, conn, barrier):
        self._conn, self._barrier = conn, barrier

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, *args):
        cursor = await self._conn.execute(sql, *args)
        if "SELECT id, data FROM channels_lite_event" in sql:
            await self._barrier.wait()
        return cursor


def layer(barrier=None):
    kwargs = {"pool_size": 1} if mode == "barrier" else {}
    result = AIOSQLiteChannelLayer(database="channels", **kwargs)
    if barrier is not None:
        connection = result.connection

        @contextlib.asynccontextmanager
        async def gated():
            async with connection() as conn:
                yield GatedConnection(conn, barrier)

        result.connection = gated
    return result


async def pull(receiver):
    try:
        return (await receiver._receive_single_from_db("work"))[1]
    except ChannelEmpty:
        return None


async def receive(receiver):
    try:
        return await asyncio.wait_for(receiver.receive("work"), 0.5)
    except TimeoutError:
        return None


async def main():
    duplicated = 0
    for n in range(rounds):
        barrier = asyncio.Barrier(2) if mode == "barrier" else None
        receivers = [layer(barrier), layer(barrier)]
        if prior:
            for i, receiver in enumerate(receivers):
                await receiver.send(f"warmup.r{i}", {"type": "warmup"})
        sender = layer()
        await sender.send("work", {"type": "job", "n": n})
        await sender.close()
        if mode == "barrier":
            got = await asyncio.gather(*(pull(r) for r in receivers))
        else:
            got = await asyncio.gather(*(receive(r) for r in receivers))
        delivered = [m for m in got if m is not None]
        assert all(m == {"type": "job", "n": n} for m in delivered), got
        assert delivered, "message was lost"
        duplicated += len(delivered) > 1
        await receivers[0].flush()
        for receiver in receivers:
            await receiver.close()
    print(json.dumps({"rounds": rounds, "duplicated": duplicated}))


asyncio.run(main())
"""

ROUNDS = 20


def repro(directory: Path, *, patch: str, prior: bool, mode: str, rounds: int = ROUNDS) -> int:
    """중복 배달된 라운드 수."""
    result = subprocess.run(
        [sys.executable, "-c", REPRO_SCRIPT, str(directory), patch, "1" if prior else "0", mode]
        + [str(rounds)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["rounds"] == rounds
    return data["duplicated"]


# --- 재현 --------------------------------------------------------------------------------


@needs_channels_lite_aio
@pytest.mark.parametrize("mode", ["barrier", "natural"])
def test_unpatched_reused_connection_duplicates_delivery(tmp_path, mode):
    # 결함이 상류에 그대로 있다는 기록. 상류가 고치면 실패해 게이트를 다시 볼 때를 알린다.
    assert repro(tmp_path, patch="none", prior=True, mode=mode) == ROUNDS


@needs_channels_lite_aio
@pytest.mark.parametrize("mode", ["barrier", "natural"])
def test_unpatched_fresh_connection_delivers_once(tmp_path, mode):
    # 대조군: 수신자 연결이 쓰기를 한 적이 없으면(total_changes == 0) 패치 없이도 한 번만 배달된다.
    assert repro(tmp_path, patch="none", prior=False, mode=mode) == 0


@needs_channels_lite_aio
@pytest.mark.parametrize("patch", ["apply", "setting"])
@pytest.mark.parametrize("mode", ["barrier", "natural"])
def test_competing_receivers_get_message_once(tmp_path, patch, mode):
    assert repro(tmp_path, patch=patch, prior=True, mode=mode) == 0


# --- 교체본이 원본과 판정만 다른지 ---------------------------------------------------------


@needs_channels_lite_aio
def test_replacement_differs_from_original_only_in_claim_check():
    script = """
import django
from django.conf import settings

settings.configure()
import inspect

from channels_lite.layers.aio import AIOSQLiteChannelLayer

print(inspect.getsource(AIOSQLiteChannelLayer._receive_single_from_db), end="")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    original = textwrap.dedent(result.stdout).splitlines()
    replacement = (ROOT / "django_sqlite_ops" / "compat" / "_channels_lite_aio.py").read_text()
    replacement = replacement[replacement.index("async def _receive_single_from_db") :].splitlines()
    changed = [
        line
        for line in unified_diff(original, replacement, lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert changed == [
        "-            await conn.execute(",
        "+            cursor = await conn.execute(",
        "-            if conn.total_changes > 0:",
        "+            if cursor.rowcount == 1:",
    ]


def test_verified_source_hash_matches_installed_original():
    if importlib.util.find_spec("channels_lite") is None:
        pytest.skip("channels-lite is not installed")
    data = run(
        """
from django_sqlite_ops.compat import channels_lite as c
st = c.status()
emit(version=st.version, applicable=st.applicable, reason=st.reason)
"""
    )
    if data["version"] != "0.4.0":
        pytest.skip(f"channels-lite {data['version']} installed; gate is for 0.4.0")
    assert data["applicable"], data["reason"]


# --- 게이트·멱등·명시 적용 ------------------------------------------------------------------

PRELUDE = """
import json
import sys

def emit(**kw):
    print(json.dumps(kw))
"""


def run(body: str, *, configure: str = "settings.configure()", before: str = "") -> dict:
    script = (
        PRELUDE
        + before
        + "\nimport django\nfrom django.conf import settings\n"
        + configure
        + "\ndjango.setup()\n"
        + textwrap.dedent(body)
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@needs_channels_lite_aio
def test_apply_is_idempotent_and_reports_applied():
    data = run(
        """
from channels_lite.layers.aio import AIOSQLiteChannelLayer as L
from django_sqlite_ops.compat import channels_lite as c
before = c.status().applied
original = L._receive_single_from_db
first = c.apply()
patched = L._receive_single_from_db
second = c.apply()
emit(
    before=before,
    first=first,
    second=second,
    same=L._receive_single_from_db is patched,
    changed=patched is not original,
    wrapped_is_original=patched.__wrapped_original__ is original,
    applied=c.status().applied,
)
"""
    )
    assert data == {
        "before": False,
        "first": True,
        "second": True,
        "same": True,
        "changed": True,
        "wrapped_is_original": True,
        "applied": True,
    }


@needs_channels_lite_aio
def test_unverified_version_is_not_patched():
    data = run(
        """
from importlib import metadata
real = metadata.version
metadata.version = lambda name: "0.4.1" if name == "channels-lite" else real(name)
from channels_lite.layers.aio import AIOSQLiteChannelLayer as L
from django_sqlite_ops.compat import channels_lite as c
original = L._receive_single_from_db
st = c.status()
emit(applied=c.apply(), same=L._receive_single_from_db is original, reason=st.reason)
"""
    )
    assert data["applied"] is False
    assert data["same"] is True
    assert "0.4.1 is not verified" in data["reason"]


@needs_channels_lite_aio
def test_changed_source_is_not_patched():
    data = run(
        """
from channels_lite.layers.aio import AIOSQLiteChannelLayer as L
from django_sqlite_ops.compat import channels_lite as c
c.VERIFIED_SOURCE_SHA256 = "0" * 64
original = L._receive_single_from_db
emit(applied=c.apply(), same=L._receive_single_from_db is original, reason=c.status().reason)
"""
    )
    assert data["applied"] is False
    assert data["same"] is True
    assert "differs from the verified" in data["reason"]


BLOCK_CHANNELS_LITE = """
from importlib import metadata
real = metadata.version

def version(name):
    if name == "channels-lite":
        raise metadata.PackageNotFoundError(name)
    return real(name)

metadata.version = version

class Block:
    def find_spec(self, name, path=None, target=None):
        if name == "channels_lite" or name.startswith("channels_lite."):
            raise ImportError(f"blocked: {name}")
        return None

sys.meta_path.insert(0, Block())
"""


def test_missing_channels_lite_is_a_silent_no_op():
    data = run(
        """
from django_sqlite_ops.compat import channels_lite as c
st = c.status()
emit(applied=c.apply(), installed=st.installed, reason=st.reason)
""",
        before=BLOCK_CHANNELS_LITE,
        configure=(
            'settings.configure(INSTALLED_APPS=["django_sqlite_ops"], '
            'SQLITE_OPS={"PATCH_CHANNELS_LITE_AIO": True})'
        ),
    )
    assert data == {
        "applied": False,
        "installed": False,
        "reason": "channels-lite is not installed",
    }


@needs_channels_lite_aio
@pytest.mark.parametrize(
    ("value", "applied"), [(True, True), (False, False), ("true", False), (None, False)]
)
def test_ready_applies_only_when_setting_is_true(tmp_path, value, applied):
    db = tmp_path / "app.sqlite3"
    ops = {} if value is None else {"PATCH_CHANNELS_LITE_AIO": value}
    databases = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(db)}}
    data = run(
        """
from django.db import connections
from django_sqlite_ops.compat import channels_lite as c
emit(applied=c.status().applied, connected=connections["default"].connection is not None)
""",
        configure=(
            'settings.configure(INSTALLED_APPS=["django_sqlite_ops"], '
            f"DATABASES={databases!r}, "
            f"SQLITE_OPS={ops!r})"
        ),
    )
    assert data == {"applied": applied, "connected": False}
    # ready() 는 DB 를 열지 않는다
    assert list(tmp_path.iterdir()) == []


def test_module_does_not_import_channels_lite_until_used():
    data = run(
        """
import django_sqlite_ops.compat.channels_lite
emit(leaked=sorted(m for m in sys.modules if m.split(".")[0] == "channels_lite"))
"""
    )
    assert data == {"leaked": []}


def test_orm_layer_checks_update_count():
    # ORM 판(layers/core.py)은 aupdate() 의 반환값(행 수)을 본다. 패치 대상이 아님을 소스로 고정.
    spec = importlib.util.find_spec("channels_lite")
    if spec is None or spec.origin is None:
        pytest.skip("channels-lite is not installed")
    source = (Path(spec.origin).parent / "layers" / "core.py").read_text()
    assert "updated = await Event.objects.filter(id=event.id, delivered=False).aupdate(" in source
    assert "if updated:" in source
    assert "total_changes" not in source


@needs_channels_lite_aio
def test_unavailable_source_is_not_patched():
    # 소스가 없는 배포(.pyc 만)에서는 원본을 대조할 수 없으므로 적용하지 않는다
    data = run(
        """
import inspect

def no_source(obj):
    raise OSError("could not get source code")

inspect.getsource = no_source
from django_sqlite_ops.compat import channels_lite as c
emit(applied=c.apply(), reason=c.status().reason)
"""
    )
    assert data["applied"] is False
    assert "not available" in data["reason"]


# --- sqlite_doctor 채널 섹션 -------------------------------------------------------------

AIO = "channels_lite.layers.aio.AIOSQLiteChannelLayer"
ORM = "channels_lite.layers.core.SQLiteChannelLayer"

DIAGNOSE = """
from dataclasses import asdict
from django_sqlite_ops.doctor import diagnose
emit(items=[asdict(i) for i in diagnose() if i.key == "patch"])
"""


def patch_items(tmp_path, *, ops=None, layers=None, before="") -> list[dict]:
    config = {
        "INSTALLED_APPS": ["django_sqlite_ops"],
        "DATABASES": {
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(tmp_path / "a.db")},
            "channels": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(tmp_path / "c.db")},
        },
    }
    if ops is not None:
        config["SQLITE_OPS"] = ops
    if layers is not None:
        config["CHANNEL_LAYERS"] = {
            "default": {"BACKEND": layers, "CONFIG": {"database": "channels"}}
        }
    data = run(DIAGNOSE, configure=f"settings.configure(**{config!r})", before=before)
    return data["items"]


ON = {"PATCH_CHANNELS_LITE_AIO": True}
UNVERIFIED = """
from importlib import metadata
real = metadata.version
metadata.version = lambda name: "0.4.1" if name == "channels-lite" else real(name)
"""


@needs_channels_lite_aio
def test_doctor_patch_applied_is_ok(tmp_path):
    [item] = patch_items(tmp_path, ops=ON, layers=AIO)
    assert (item["alias"], item["level"], item["value"]) == ("default", "ok", "0.4.0")
    assert "applied" in item["message"]


@needs_channels_lite_aio
def test_doctor_patch_off_warns_with_setting_hint(tmp_path):
    [item] = patch_items(tmp_path, layers=AIO)
    assert item["level"] == "warn"
    assert "SQLITE_OPS['PATCH_CHANNELS_LITE_AIO'] = True" in item["message"]


@needs_channels_lite_aio
@pytest.mark.parametrize("ops", [None, ON])
def test_doctor_unverified_version_warns_with_reason(tmp_path, ops):
    [item] = patch_items(tmp_path, ops=ops, layers=AIO, before=UNVERIFIED)
    assert (item["level"], item["value"], item["expected"]) == (
        "warn",
        "0.4.1",
        "channels-lite==0.4.0",
    )
    assert "0.4.1 is not verified" in item["message"]


def test_doctor_setting_without_channels_lite_warns(tmp_path):
    [item] = patch_items(tmp_path, ops=ON, before=BLOCK_CHANNELS_LITE)
    assert (item["alias"], item["level"]) == (None, "warn")
    assert "channels-lite is not installed" in item["message"]


@pytest.mark.parametrize("layers", [None, ORM])
def test_doctor_no_patch_item_without_aio_layer_or_setting(tmp_path, layers):
    assert patch_items(tmp_path, layers=layers) == []
