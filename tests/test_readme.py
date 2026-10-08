"""README 의 python 코드 예가 실제로 동작하는지 확인한다 (README 는 활용 가이드다, #11)."""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _litestream import needs_litestream

from django_sqlite_ops.boot import cli
from django_sqlite_ops.database import recommended, sqlite_database

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
PYTHON_BLOCKS = re.findall(r"^ *```python\n(.*?)^ *```", README, flags=re.S | re.M)


def test_readme_has_python_examples():
    assert len(PYTHON_BLOCKS) >= 5


def _settings_file(tmp_path):
    # BASE_DIR = Path(__file__).resolve().parent.parent 가 tmp_path 를 가리키게 한다
    settings_file = tmp_path / "proj" / "settings.py"
    settings_file.parent.mkdir()
    return settings_file


@pytest.mark.parametrize("index", range(len(PYTHON_BLOCKS)))
def test_readme_python_block_runs(index, tmp_path):
    source = PYTHON_BLOCKS[index]
    tree = ast.parse(source)
    if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr):
        # 출력 예시 dict 는 실제 출력과 같아야 한다
        shown = ast.literal_eval(tree.body[0].value)
        assert shown == sqlite_database(shown["NAME"])
        return
    namespace = {"__file__": str(_settings_file(tmp_path)), "__name__": "readme_example"}
    exec(compile(source, f"README.md python block {index}", "exec"), namespace)


CONNECT_SCRIPT = """
import json
import sys
from pathlib import Path

import django
from django.conf import settings

databases = json.loads(sys.argv[1])
settings.configure(DATABASES=databases, USE_TZ=True)
django.setup()

from django.db import connections

result = {}
for alias in databases:
    with connections[alias].cursor() as cursor:
        cursor.execute("CREATE TABLE t (x INTEGER)")
        cursor.execute("INSERT INTO t VALUES (1)")
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        cursor.execute("PRAGMA busy_timeout")
        busy = cursor.fetchone()[0]
    name = Path(databases[alias]["NAME"])
    # 연결이 열린 동안 WAL 파일이 함께 있다 (README 문제 해결)
    sidecars = [Path(f"{name}-wal").exists(), Path(f"{name}-shm").exists()]
    result[alias] = [mode, busy, sidecars]
print(json.dumps(result))
"""


@pytest.mark.parametrize(
    "index",
    [
        i
        for i, block in enumerate(PYTHON_BLOCKS)
        if "DATABASES = {" in block and "SQLITE_OPS" not in block
    ],
)
def test_readme_databases_example_connects(index, tmp_path):
    namespace = {"__file__": str(_settings_file(tmp_path)), "__name__": "readme_example"}
    exec(PYTHON_BLOCKS[index], namespace)
    databases = namespace["DATABASES"]
    for config in databases.values():
        assert Path(config["NAME"]).parent == tmp_path
    result = subprocess.run(
        [sys.executable, "-c", CONNECT_SCRIPT, json.dumps(databases)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for alias, (mode, busy, sidecars) in json.loads(result.stdout).items():
        expected_busy = 10000 if alias == "events" else 5000
        assert (mode, busy, sidecars) == ("wal", expected_busy, [True, True]), alias


CHECK_SCRIPT = """
import json
import sys

import django
from django.conf import settings

config = json.loads(sys.argv[1])
settings.configure(USE_TZ=True, **config)
django.setup()

from django.core.checks import run_checks

ids = [m.id for m in run_checks(include_deployment_checks=True) if m.id.startswith("sqlite_ops.")]
print(json.dumps(ids))
"""

CHECK_BLOCKS = [i for i, block in enumerate(PYTHON_BLOCKS) if "SQLITE_OPS" in block]


def test_readme_has_check_example():
    assert CHECK_BLOCKS


@pytest.mark.parametrize("index", CHECK_BLOCKS)
def test_readme_check_example_has_no_warnings(index, tmp_path):
    # 시스템 체크 절의 settings 예는 check --deploy 에서 sqlite_ops 경고가 없어야 한다
    namespace = {"__file__": str(_settings_file(tmp_path)), "__name__": "readme_example"}
    exec(PYTHON_BLOCKS[index], namespace)
    config = {key: namespace[key] for key in ("INSTALLED_APPS", "SQLITE_OPS", "DATABASES")}
    assert "django_sqlite_ops" in config["INSTALLED_APPS"]
    result = subprocess.run(
        [sys.executable, "-c", CHECK_SCRIPT, json.dumps(config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_readme_check_ids_match_code():
    section = README.split("### 시스템 체크", 1)[1].split("\n### ", 1)[0]
    shown = set(re.findall(r"^\| `(sqlite_ops\.[EW]\d{3})` \|", section, flags=re.M))
    source = (ROOT / "django_sqlite_ops" / "checks.py").read_text(encoding="utf-8")
    assert shown == set(re.findall(r'id="(sqlite_ops\.[EW]\d{3})"', source))


def test_readme_default_table_matches_code():
    section = README.split("#### 기본값", 1)[1].split("\n#### ", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("`"):
            rows[cells[0].strip("`")] = cells[1]
    rec = recommended()
    code = {"transaction_mode": rec["transaction_mode"], **rec["pragmas"]}
    assert set(rows) == set(code)
    for name, value in code.items():
        assert rows[name].startswith(f"`{value}`"), (name, rows[name])


def test_readme_boot_exit_codes_match_code():
    section = README.split("#### 종료 코드", 1)[1].split("\n#### ", 1)[0]
    shown = set(re.findall(r"^\| `(\d+)` \|", section, flags=re.M))
    codes = {
        cli.EXIT_REFUSE,
        cli.EXIT_INTEGRITY,
        cli.EXIT_RESTORE,
        cli.EXIT_LOCK,
        cli.EXIT_USAGE,
        cli.EXIT_EXEC,
    }
    assert shown == {str(c) for c in codes}


# --- 빠른 시작: settings·litestream.yml·entrypoint 가 한 흐름으로 맞물리는지 ---------------

QUICK_START = README.split("## 빠른 시작", 1)[1].split("\n## ", 1)[0]
QUICK_PYTHON = re.findall(r"^```python\n(.*?)^```", QUICK_START, flags=re.S | re.M)
QUICK_YAML = re.findall(r"^```yaml\n(.*?)^```", QUICK_START, flags=re.S | re.M)
QUICK_SHELL = re.findall(r"^```bash\n(.*?)^```", QUICK_START, flags=re.S | re.M)
SHELL_BLOCKS = re.findall(r"^ *```bash\n(.*?)^ *```", README, flags=re.S | re.M)
QUICK_DB = "/data/app.sqlite3"
QUICK_CONFIG = "/etc/litestream.yml"


def _quick_settings() -> dict:
    namespace: dict = {}
    exec(next(b for b in QUICK_PYTHON if "SQLITE_OPS" in b), namespace)
    return namespace


def test_quick_start_has_every_piece():
    assert any("SQLITE_OPS" in b for b in QUICK_PYTHON)
    assert any("health_view" in b for b in QUICK_PYTHON)
    assert len(QUICK_YAML) == 1
    assert any(b.startswith("#!/bin/sh\n") for b in QUICK_SHELL)
    assert "check --deploy" in QUICK_START
    assert "sqlite_doctor" in QUICK_START


def test_quick_start_uses_one_real_path():
    # settings 의 NAME, health 의 litestream_config, boot 의 --db·--config 가 같은 값이다 (D-15)
    settings = _quick_settings()
    assert settings["DATABASES"]["default"]["NAME"] == QUICK_DB
    assert settings["SQLITE_OPS"]["HEALTH"]["DATABASES"]["default"] == {
        "litestream_config": QUICK_CONFIG
    }
    entrypoint = next(b for b in QUICK_SHELL if b.startswith("#!/bin/sh\n"))
    assert f"--db {QUICK_DB}" in entrypoint
    assert f"--config {QUICK_CONFIG}" in entrypoint
    assert f"-- litestream replicate -config {QUICK_CONFIG}" in entrypoint
    # 한 번만 쓰는 옵션은 굳히지 않고 BOOT_FLAGS 로만 넘긴다
    for flag in ("--init-new", "--adopt-existing", "--on-unknown"):
        assert flag not in entrypoint.split("set -eu", 1)[1]
    assert QUICK_YAML[0].startswith(f"# {QUICK_CONFIG}\n")


def test_quick_start_litestream_yml_parses():
    yaml = pytest.importorskip("yaml")
    dbs = yaml.safe_load(QUICK_YAML[0])["dbs"]
    assert [db["path"] for db in dbs] == [QUICK_DB]


@needs_litestream
def test_quick_start_litestream_yml_is_read_by_litestream(tmp_path, litestream_binary):
    config = tmp_path / "litestream.yml"
    config.write_text(QUICK_YAML[0], encoding="utf-8")
    result = subprocess.run(
        [litestream_binary, "databases", "-config", str(config), "-json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert [db["path"] for db in json.loads(result.stdout)] == [QUICK_DB]


@pytest.mark.parametrize("shell", ["bash", "sh"])
@pytest.mark.parametrize("index", range(len(SHELL_BLOCKS)))
def test_readme_shell_block_syntax(index, shell, tmp_path):
    script = tmp_path / "block.sh"
    # 자리 표시(`[boot 옵션]`)는 문법 검사에서 낱말로 바꾼다
    script.write_text(SHELL_BLOCKS[index].replace("[boot 옵션]", "OPTIONS"), encoding="utf-8")
    result = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
