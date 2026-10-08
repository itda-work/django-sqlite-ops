"""Litestream CLI 호출: 원격·로컬 최대 TXID 조회와 restore (DESIGN §4-3).

표준 라이브러리만 쓴다. Django 를 import 하지 않는다(DESIGN §4-1).

공개 함수는 예외를 밖으로 던지지 않는다. 원격 조회는 ``RemoteError`` / ``RemoteEmpty`` /
``RemoteTxid`` 중 하나를, 로컬 조회는 ``int | None`` 을, restore 는 ``RestoreResult`` 를
돌려준다. 판정은 ``decide()`` 가 한다.

빈 목록(rc 0, ``[]``)은 그대로 ``RemoteEmpty`` 다. 0.5.17 은 복제본 경로·prefix 오타와
"복제본 없음"을 같은 출력으로 돌려주므로 이 모듈은 둘을 구분하지 못한다. 그 빈 목록을 새 DB 의
근거로 쓸지는 판정 정책이 정한다(``decide()`` 의 ``init_new``, D-13).

출력 형식·동작은 Litestream 0.5.17 에서 실측했다(``tests/fixtures/litestream-0.5.17/``).
검증하지 않은 버전이면 원격 조회와 restore 를 하지 않고 실패로 돌려준다.
"""

import json
import os
import re
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .decide import Remote, RemoteEmpty, RemoteError, RemoteTxid

__all__ = [
    "DEFAULT_LTX_TIMEOUT",
    "DEFAULT_RESTORE_TIMEOUT",
    "DEFAULT_VERSION_TIMEOUT",
    "VERIFIED_VERSIONS",
    "RestoreResult",
    "check_version",
    "default_meta_path",
    "local_max_txid",
    "parse_ltx_json",
    "parse_version",
    "remote_max_txid",
    "restore",
    "version",
]

# 실측으로 출력 형식과 동작을 확인한 버전. 범위 밖이면 판정할 수 없으므로 거부한다.
VERIFIED_VERSIONS = frozenset({"0.5.17"})

# `litestream version` 은 네트워크를 쓰지 않는다. 바이너리가 멈췄을 때만 걸린다.
DEFAULT_VERSION_TIMEOUT = 10.0
# `ltx` 는 목록 조회 한 번이다(file:// 수 ms, 정상 S3 수 초). endpoint 에 http:// 가 빠지면
# 응답 없이 무한 대기하므로(실측) 부팅을 붙잡지 않도록 짧게 끊는다. 실패는 거부(exit 2)로 이어져
# 운영자가 원인을 보게 된다.
DEFAULT_LTX_TIMEOUT = 30.0
# restore 는 DB 크기에 비례한다. 수 GB 를 내려받을 수 있는 시간으로 넉넉히 잡고 호출자가 바꾼다.
DEFAULT_RESTORE_TIMEOUT = 600.0

# 타임아웃 뒤 프로세스 그룹을 죽이고 파이프가 닫히기를 기다리는 시간.
_KILL_GRACE = 5.0
# 사유 한 줄의 최대 길이.
_MAX_REASON = 300

# ltx.FormatFilename / TXID.String(): 16자리 소문자 16진수 (superfly/ltx v0.5.2).
_TXID_RE = re.compile(r"[0-9a-f]{16}")
_LTX_NAME_RE = re.compile(r"^([0-9a-f]{16})-([0-9a-f]{16})\.ltx$")

# LTX 헤더(superfly/ltx v0.5.2 ltx.go — Magic:20, HeaderSize:28, HeaderFlagNoChecksum:175,
# Header.MarshalBinary:283, IsValidPageSize:399, MaxPageSize:396). 모두 big-endian.
#   [0:4] "LTX1"  [4:8] flags  [8:12] page size  [16:24] min TXID  [24:32] max TXID
_LTX_MAGIC = b"LTX1"
_LTX_HEADER_SIZE = 100
_LTX_FLAG_MASK = 1 << 1  # HeaderFlagNoChecksum 만 정의돼 있다
_LTX_PAGE_SIZES = frozenset(1 << n for n in range(9, 17))  # 512 … 65536
# Litestream(slog) 로그 줄. `restore -integrity-check` 는 이 줄을 JSON 앞 stdout 에 쓴다(실측).
_LOG_LINE_RE = re.compile(r"^time=\S+ level=[A-Z]+ ")
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """restore 결과. ``ok`` 이면 ``txid`` 는 복원된 최대 TXID 다."""

    ok: bool
    reason: str
    txid: int | None = None


@dataclass(frozen=True, slots=True)
class _Run:
    rc: int
    stdout: str
    stderr: str


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    if len(line) > _MAX_REASON:
        line = line[: _MAX_REASON - 3] + "..."
    return line


def _last_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return _one_line(lines[-1]) if lines else ""


def _kill(proc: subprocess.Popen) -> None:
    # 자식이 만든 손자 프로세스가 파이프를 쥐고 있으면 communicate() 가 끝나지 않는다.
    # 새 세션으로 띄웠으므로 그룹 전체를 죽인다.
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows
            proc.kill()
    except OSError:
        pass


def _run(argv: list[str], timeout: float) -> _Run | str:
    """명령을 실행한다. 실행하지 못했거나 시간이 넘으면 사유 문자열을 돌려준다."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return _one_line(f"cannot run {argv[0]}: {exc}")
    except ValueError as exc:
        # 경로에 NUL 등 OS 에 넘길 수 없는 값이 있다. 인자를 그대로 쓰지 않는다(제어 문자).
        return _one_line(f"invalid litestream argument: {exc}")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            proc.communicate(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        return f"{argv[0]} {argv[1]} timed out after {timeout:g}s"
    return _Run(
        rc=proc.returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
    )


def _failure(cmd: str, run: _Run) -> str:
    detail = _last_line(run.stderr) or _last_line(run.stdout) or "no output"
    return _one_line(f"litestream {cmd} exited with {run.rc}: {detail}")


def _strip_log_lines(stdout: str) -> str:
    lines = stdout.splitlines()
    while lines and (not lines[0].strip() or _LOG_LINE_RE.match(lines[0])):
        lines.pop(0)
    return "\n".join(lines)


# --- 버전 ------------------------------------------------------------------------------


def parse_version(stdout: str) -> str | None:
    """``litestream version`` 출력에서 ``X.Y.Z`` 를 읽는다. 형식이 다르면 ``None``."""
    m = _VERSION_RE.match(stdout.strip())
    return m.group(1) if m else None


def version(*, binary: str = "litestream", timeout: float = DEFAULT_VERSION_TIMEOUT) -> str | None:
    """``litestream version`` 의 ``X.Y.Z``. 실행·파싱에 실패하면 ``None`` (기록용, 검증 아님)."""
    run = _run([binary, "version"], timeout)
    if isinstance(run, str) or run.rc != 0:
        return None
    return parse_version(run.stdout)


def check_version(
    *, binary: str = "litestream", timeout: float = DEFAULT_VERSION_TIMEOUT
) -> str | None:
    """검증한 버전이면 ``None``, 아니면(읽지 못한 경우 포함) 사유 한 줄."""
    run = _run([binary, "version"], timeout)
    if isinstance(run, str):
        return run
    if run.rc != 0:
        return _failure("version", run)
    version = parse_version(run.stdout)
    if version is None:
        return f"cannot parse litestream version output: {_one_line(run.stdout)!r}"
    if version not in VERIFIED_VERSIONS:
        verified = ", ".join(sorted(VERIFIED_VERSIONS))
        return f"unsupported litestream version {version}; verified: {verified}"
    return None


# --- 원격 TXID ------------------------------------------------------------------------


def parse_ltx_json(stdout: str) -> Remote:
    """``litestream ltx -level all -json`` 의 stdout 을 원격 결과로 바꾼다.

    빈 배열은 ``RemoteEmpty``, 항목이 있으면 모든 레벨의 ``max_txid`` 최대값을 ``RemoteTxid`` 로.
    JSON 이 아니거나 스키마가 예상과 다르면 ``RemoteError``.
    """
    body = _strip_log_lines(stdout)
    try:
        data = json.loads(body)
    except ValueError as exc:
        return RemoteError(_one_line(f"cannot parse litestream ltx output as JSON: {exc}"))
    if type(data) is not list:
        return RemoteError(
            f"unexpected litestream ltx output: expected a list, got {type(data).__name__}"
        )
    best: int | None = None
    for i, item in enumerate(data):
        if type(item) is not dict:
            return RemoteError(f"unexpected litestream ltx output: item {i} is not an object")
        level = item.get("level")
        min_txid = item.get("min_txid")
        max_txid = item.get("max_txid")
        if type(level) is not int or not 0 <= level <= 9:
            return RemoteError(f"unexpected litestream ltx output: item {i} has invalid level")
        if type(min_txid) is not str or not _TXID_RE.fullmatch(min_txid):
            return RemoteError(f"unexpected litestream ltx output: item {i} has invalid min_txid")
        if type(max_txid) is not str or not _TXID_RE.fullmatch(max_txid):
            return RemoteError(f"unexpected litestream ltx output: item {i} has invalid max_txid")
        lo, hi = int(min_txid, 16), int(max_txid, 16)
        if lo > hi:
            return RemoteError(
                f"unexpected litestream ltx output: item {i} has min_txid > max_txid"
            )
        best = hi if best is None else max(best, hi)
    if best is None:
        return RemoteEmpty()
    return RemoteTxid(best)


def remote_max_txid(
    db_path: str | os.PathLike[str],
    *,
    config: str | os.PathLike[str],
    binary: str = "litestream",
    timeout: float = DEFAULT_LTX_TIMEOUT,
    version_timeout: float = DEFAULT_VERSION_TIMEOUT,
) -> Remote:
    """설정 파일의 복제본에서 모든 레벨의 최대 TXID 를 조회한다.

    ``litestream ltx -level all -json`` 을 쓴다. 기본(``-level`` 생략)은 L0 만 나열해
    L0 가 지워진 복제본을 빈 목록으로 오판한다(DESIGN §4-4).

    주의(0.5.17 실측): 복제본 경로가 없거나 비어 있어도 rc 0 과 ``[]`` 다. 둘은 출력으로
    구분되지 않으므로 둘 다 ``RemoteEmpty`` 다. 설정에 없는 DB·설정 파일 없음·접근 불가는 rc 1.
    """
    problem = check_version(binary=binary, timeout=version_timeout)
    if problem is not None:
        return RemoteError(problem)
    argv = [
        binary,
        "ltx",
        "-config",
        os.fspath(config),
        "-level",
        "all",
        "-json",
        os.fspath(db_path),
    ]
    run = _run(argv, timeout)
    if isinstance(run, str):
        return RemoteError(run)
    if run.rc != 0:
        return RemoteError(_failure("ltx", run))
    return parse_ltx_json(run.stdout)


# --- 로컬 TXID ------------------------------------------------------------------------


def default_meta_path(db_path: str | os.PathLike[str]) -> Path:
    """Litestream 기본 메타 디렉터리 ``<dir>/.<name>-litestream`` (설정의 meta-path 가 없을 때)."""
    db = Path(db_path)
    return db.parent / f".{db.name}-litestream"


def _read_ltx_range(path: Path) -> tuple[int, int] | None:
    """LTX 파일 헤더의 (min TXID, max TXID). 정규 파일·헤더 형식이 아니면 ``None``.

    LTX 파일 자체가 심볼릭 링크이면 거부한다. Litestream 은 ``ltx/0`` 안에 파일 링크를 만들지
    않으므로 예상 밖 상태다. 상위 디렉터리(메타 디렉터리·``ltx``·``ltx/0``)의 링크는 허용하고
    따라간다(볼륨 연결 등). FIFO·장치 파일에서 멈추지 않도록 열기 전후로 정규 파일인지 본다.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            header = b""
            while len(header) < _LTX_HEADER_SIZE:
                chunk = os.read(fd, _LTX_HEADER_SIZE - len(header))
                if not chunk:
                    break
                header += chunk
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None
    if len(header) < _LTX_HEADER_SIZE or header[:4] != _LTX_MAGIC:
        return None
    if int.from_bytes(header[4:8], "big") & ~_LTX_FLAG_MASK:
        return None
    if int.from_bytes(header[8:12], "big") not in _LTX_PAGE_SIZES:
        return None
    return int.from_bytes(header[16:24], "big"), int.from_bytes(header[24:32], "big")


def local_max_txid(
    db_path: str | os.PathLike[str], *, meta_path: str | os.PathLike[str] | None = None
) -> int | None:
    """로컬 메타의 L0 에서 최신 LTX 파일의 max TXID 를 읽는다. 믿을 수 없으면 ``None``.

    후보 고르기는 Litestream 0.5.17 ``DB.MaxLTX()`` 와 같다: ``<meta>/ltx/0/`` 의 파일 이름
    ``<min>-<max>.ltx`` 중 ``max`` 가 가장 큰 것(같으면 이름순 첫 번째). L0 보존 정리는 가장 새
    L0 파일을 지우지 않으므로 업로드·압축·종료 뒤에도 남는다(db.go ``EnforceL0RetentionByTime``,
    실측).

    고른 후보 **하나**를 검증한다. 정규 파일(파일 자체가 링크면 거부, 상위 디렉터리 링크는
    허용), 읽기 가능, 1 ≤ min ≤ max, LTX 헤더의 매직·flags·page size 가 유효하고 헤더의
    min/max 가 이름과 같아야 한다. 하나라도 어긋나면 낮은 후보로 내려가지 않고 ``None`` 이다.
    체크섬·페이지 전체 검증(Litestream ``DB.Pos()`` 의 ``Decoder.Verify()``)은 하지 않는다.
    """
    try:
        meta = Path(meta_path) if meta_path is not None else default_meta_path(db_path)
        l0 = meta / "ltx" / "0"
        names = os.listdir(l0)
    except (OSError, ValueError):
        return None
    best: tuple[int, int, str] | None = None
    for name in sorted(names):
        m = _LTX_NAME_RE.match(name)
        if m is None:
            continue
        lo, hi = int(m.group(1), 16), int(m.group(2), 16)
        if best is None or hi > best[1]:
            best = (lo, hi, name)
    if best is None:
        return None
    lo, hi, name = best
    if not 1 <= lo <= hi:
        return None
    if _read_ltx_range(l0 / name) != (lo, hi):
        return None
    return hi


# --- restore --------------------------------------------------------------------------


def _parse_restore_json(stdout: str) -> int | str:
    """restore ``-json`` 요약에서 txid 를 읽는다. 실패면 사유 문자열."""
    body = _strip_log_lines(stdout)
    try:
        data = json.loads(body)
    except ValueError as exc:
        return _one_line(f"cannot parse litestream restore output as JSON: {exc}")
    if type(data) is not dict:
        return "unexpected litestream restore output: expected an object"
    txid = data.get("txid")
    if type(txid) is not str or not _TXID_RE.fullmatch(txid):
        return "unexpected litestream restore output: invalid txid"
    return int(txid, 16)


def restore(
    db_path: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    config: str | os.PathLike[str],
    binary: str = "litestream",
    timeout: float = DEFAULT_RESTORE_TIMEOUT,
    version_timeout: float = DEFAULT_VERSION_TIMEOUT,
    integrity_check: str = "quick",
) -> RestoreResult:
    """설정 파일의 복제본을 ``output`` 경로로 복원하고 성공을 확인한다.

    ``output`` 은 아직 없는 경로여야 한다(``-force`` 를 쓰지 않는다). 실제 DB 교체·격리는
    호출자 몫이다. ``-if-db-not-exists`` 는 쓰지 않는다: 0바이트 DB 파일이 있으면 rc 0 으로
    복원을 건너뛴다(0.5.17 실측, DESIGN §4-1). 실패·타임아웃 때 ``output`` 에 남은 파일은
    지우지 않는다.
    """
    out = os.fspath(output)
    if os.path.lexists(out):
        return RestoreResult(False, f"restore output already exists: {out}")
    if integrity_check not in ("none", "quick", "full"):
        return RestoreResult(False, f"invalid integrity_check {integrity_check!r}")
    problem = check_version(binary=binary, timeout=version_timeout)
    if problem is not None:
        return RestoreResult(False, problem)
    argv = [
        binary,
        "restore",
        "-config",
        os.fspath(config),
        "-json",
        "-integrity-check",
        integrity_check,
        "-o",
        out,
        os.fspath(db_path),
    ]
    run = _run(argv, timeout)
    if isinstance(run, str):
        return RestoreResult(False, run)
    if run.rc != 0:
        return RestoreResult(False, _failure("restore", run))
    txid = _parse_restore_json(run.stdout)
    if isinstance(txid, str):
        return RestoreResult(False, txid)
    try:
        size = os.path.getsize(out)
    except OSError as exc:
        return RestoreResult(False, _one_line(f"restored file is missing: {exc}"))
    if size == 0:
        return RestoreResult(False, f"restored file is empty: {out}")
    return RestoreResult(True, f"restored txid {txid} to {out}", txid)
