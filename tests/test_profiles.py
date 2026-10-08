"""배포 프로필 문서(docs/profiles/*.md)의 조각이 실제로 동작하는지 확인한다 (#9).

- python 조각: 그대로 실행한다. settings 조각은 ``check --deploy`` 에서 ``sqlite_ops.*`` 가
  없어야 한다. ``channels`` 를 import 하지 못하게 막은 상태에서도 같아야 한다
  (``CHANNEL_LAYERS`` 는 문자열로만 읽힌다).
- ``litestream.yml`` 조각: 실제 litestream 의 ``litestream databases -config`` 가 읽어야 한다.
- 셸 조각: ``bash -n``·``sh -n``.
- compose 조각: YAML 구문만(컨테이너 종단 검증은 #10 회귀 랩).
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _litestream import needs_litestream

from django_sqlite_ops.database import PROFILES

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "docs" / "profiles"
DOCS = {name: (PROFILE_DIR / f"{name}.md").read_text(encoding="utf-8") for name in PROFILES}


def _blocks(lang: str) -> list[tuple[str, int, str]]:
    found = []
    for name, text in DOCS.items():
        blocks = re.findall(rf"^```{lang}\n(.*?)^```", text, flags=re.S | re.M)
        found.extend((name, i, block) for i, block in enumerate(blocks))
    return found


def _ids(blocks):
    return [f"{name}-{i}" for name, i, _ in blocks]


PYTHON = _blocks("python")
SETTINGS = [b for b in PYTHON if "SQLITE_OPS" in b[2]]
YAML = _blocks("yaml")
LITESTREAM_YAML = [b for b in YAML if b[2].lstrip().startswith("# /etc/litestream.yml")]
COMPOSE_YAML = [b for b in YAML if "services:" in b[2]]
SHELL = _blocks("bash")


def test_every_profile_has_a_document():
    assert set(DOCS) == set(PROFILES)


def test_documents_have_the_expected_snippets():
    for name in PROFILES:
        assert [b for b in SETTINGS if b[0] == name], name
        assert [b for b in SHELL if b[0] == name], name
        assert [b for b in COMPOSE_YAML if b[0] == name], name
    # litestream.yml 은 single-server 에 하나만 두고 multiproc 은 링크한다
    assert [b[0] for b in LITESTREAM_YAML] == ["single-server"]


@pytest.mark.parametrize("block", PYTHON, ids=_ids(PYTHON))
def test_python_block_runs(block):
    name, index, source = block
    ast.parse(source)
    exec(compile(source, f"docs/profiles/{name}.md python block {index}", "exec"), {})


CHECK_SCRIPT = """
import json
import sys

block_channels = sys.argv[2] == "1"
if block_channels:
    class BlockChannels:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in {"channels", "channels_nats", "channels_redis"}:
                raise ImportError(f"blocked: {name}")
            return None

    sys.meta_path.insert(0, BlockChannels())

import django
from django.conf import settings

config = json.loads(sys.argv[1])
settings.configure(USE_TZ=True, **config)
django.setup()

from django.core.checks import run_checks

ids = [m.id for m in run_checks(include_deployment_checks=True) if m.id.startswith("sqlite_ops.")]
leaked = sorted(
    m for m in sys.modules if m.split(".")[0] in {"channels", "channels_nats", "channels_redis"}
)
print(json.dumps({"ids": ids, "leaked": leaked}))
"""

SETTINGS_KEYS = ("INSTALLED_APPS", "SQLITE_OPS", "DATABASES", "CHANNEL_LAYERS", "ASGI_APPLICATION")


def _settings(source: str) -> dict:
    namespace: dict = {}
    exec(source, namespace)
    return {key: namespace[key] for key in SETTINGS_KEYS if key in namespace}


@pytest.mark.parametrize("block_channels", [False, True], ids=["channels-allowed", "no-channels"])
@pytest.mark.parametrize("block", SETTINGS, ids=_ids(SETTINGS))
def test_settings_block_passes_check_deploy(block, block_channels):
    name, _, source = block
    config = _settings(source)
    assert "django_sqlite_ops" in config["INSTALLED_APPS"]
    assert "CHANNEL_LAYERS" in config
    # 문서의 프로필과 설정의 프로필이 같다
    assert config["SQLITE_OPS"]["PROFILE"] == name
    assert config["SQLITE_OPS"]["HEALTH"]["DATABASES"]["default"] == {
        "litestream_config": "/etc/litestream.yml"
    }
    result = subprocess.run(
        [sys.executable, "-c", CHECK_SCRIPT, json.dumps(config), "1" if block_channels else "0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ids"] == []
    # 시스템 체크는 CHANNEL_LAYERS 를 문자열로만 읽는다: 채널 패키지를 import 하지 않는다
    assert out["leaked"] == []


def test_settings_use_the_documented_channel_layers():
    backends = {
        (name, _settings(source)["CHANNEL_LAYERS"]["default"]["BACKEND"])
        for name, _, source in SETTINGS
    }
    assert backends == {
        ("single-server", "channels.layers.InMemoryChannelLayer"),
        ("single-server-multiproc", "channels_nats.NatsChannelLayer"),
        ("single-server-multiproc", "channels_redis.core.RedisChannelLayer"),
    }


DOCTOR_SCRIPT = """
import json
import sys

import django
from django.conf import settings

config = json.loads(sys.argv[1])
settings.configure(USE_TZ=True, **config)
django.setup()

from django_sqlite_ops import doctor

items = doctor._channel_items({}, None, config["SQLITE_OPS"]["PROFILE"])
print(json.dumps([[i.level, i.value] for i in items]))
"""


@pytest.mark.parametrize("block", SETTINGS, ids=_ids(SETTINGS))
def test_doctor_accepts_the_channel_layer(block):
    # 문서가 권하는 레이어는 sqlite_doctor 의 channels 섹션에서 ok 다
    config = _settings(block[2])
    result = subprocess.run(
        [sys.executable, "-c", DOCTOR_SCRIPT, json.dumps(config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout)
    assert items == [["ok", config["CHANNEL_LAYERS"]["default"]["BACKEND"]]]


def _paths_in_snippets() -> set[str]:
    paths = set()
    for _, _, source in SETTINGS:
        paths.add(str(_settings(source)["DATABASES"]["default"]["NAME"]))
    return paths


@needs_litestream
@pytest.mark.parametrize("block", LITESTREAM_YAML, ids=_ids(LITESTREAM_YAML))
def test_litestream_yml_is_read_by_litestream(block, tmp_path, litestream_binary):
    config = tmp_path / "litestream.yml"
    config.write_text(block[2], encoding="utf-8")
    result = subprocess.run(
        [litestream_binary, "databases", "-config", str(config), "-json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    dbs = json.loads(result.stdout)
    assert [db["path"] for db in dbs] == ["/data/app.sqlite3"]
    assert [db["replica"] for db in dbs] == ["s3"]
    # settings·boot·litestream 이 같은 실제 경로를 쓴다 (D-15)
    assert _paths_in_snippets() == {"/data/app.sqlite3"}


@pytest.mark.parametrize("block", LITESTREAM_YAML, ids=_ids(LITESTREAM_YAML))
def test_litestream_endpoint_has_scheme(block):
    # endpoint 에 스킴이 빠지면 HTTPS 로 접속해 무한 대기한다 (CLAUDE.md 함정)
    yaml = pytest.importorskip("yaml")
    replica = yaml.safe_load(block[2])["dbs"][0]["replica"]
    assert replica["endpoint"].startswith(("http://", "https://"))


@pytest.mark.parametrize("block", COMPOSE_YAML, ids=_ids(COMPOSE_YAML))
def test_compose_block_is_valid_yaml(block):
    yaml = pytest.importorskip("yaml")
    services = yaml.safe_load(block[2])["services"]
    assert services["app"]["volumes"] == ["appdata:/data"]
    assert services["app"]["environment"]["BOOT_FLAGS"] == ""


@pytest.mark.parametrize("shell", ["bash", "sh"])
@pytest.mark.parametrize("block", SHELL, ids=_ids(SHELL))
def test_shell_block_syntax(block, shell, tmp_path):
    script = tmp_path / "entrypoint.sh"
    script.write_text(block[2], encoding="utf-8")
    result = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("block", SHELL, ids=_ids(SHELL))
def test_entrypoint_matches_boot_contract(block):
    source = block[2]
    assert source.startswith("#!/bin/sh\n")
    assert "exec python -m django_sqlite_ops.boot" in source
    assert "--db /data/app.sqlite3" in source
    assert "--config /etc/litestream.yml" in source
    # 한 번만 쓰는 옵션은 이미지에 굳히지 않고 BOOT_FLAGS 로만 넘긴다
    for flag in ("--init-new", "--adopt-existing", "--on-unknown"):
        assert flag not in source.split("set -eu", 1)[1]
    assert "-- litestream replicate -config /etc/litestream.yml" in source


def _table(text: str, header: str) -> list[str]:
    start = text.index(header)
    lines = []
    for line in text[start:].splitlines():
        line = line.strip()
        if not line.startswith("|"):
            break
        lines.append(line)
    return lines


def test_layer_semantics_table_matches_design():
    # DESIGN §8 의 의미론 표를 숨기거나 바꾸지 않고 옮긴다
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    header = "| | channels-nats | channels_redis |"
    design_table = _table(design, header)
    doc_table = _table(DOCS["single-server-multiproc"], header)
    assert len(doc_table) >= 6
    assert doc_table == design_table


DOCKERFILE = _blocks("dockerfile")
REQUIREMENTS = [b for b in _blocks("text") if "django-sqlite-ops @" in b[2]]


def test_dockerfile_prepares_db_parent_directory():
    # boot 는 없는 부모를 exit 64 로 거부한다(D-15). 볼륨 없는 배포에서도 /data 가 있어야 한다
    assert [b[0] for b in DOCKERFILE] == ["single-server"]
    source = DOCKERFILE[0][2]
    assert "mkdir -p /data" in source
    assert "chown app:app /data" in source
    assert re.search(r"^USER app$", source, flags=re.M)
    # 디렉터리를 만든 뒤에 앱 사용자로 바꾼다
    assert source.index("mkdir -p /data") < source.index("\nUSER app")


def test_requirements_include_websocket_support():
    # uvicorn 만으로는 웹소켓 구현이 없다. [standard] 가 websockets 를 넣는다
    assert [b[0] for b in REQUIREMENTS] == ["single-server"]
    lines = REQUIREMENTS[0][2].split()
    assert "uvicorn[standard]" in lines
    assert "uvicorn" not in lines
