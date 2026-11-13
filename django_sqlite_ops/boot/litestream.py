"""Litestream CLI 호출: 원격·로컬 최대 TXID 조회와 restore (DESIGN §4-3).

표준 라이브러리만 쓴다. Django 를 import 하지 않는다(DESIGN §4-1).

공개 함수는 예외를 밖으로 던지지 않는다. 원격 조회는 ``RemoteError`` / ``RemoteEmpty`` /
``RemoteTxid`` 중 하나를, 로컬 조회는 ``int | None`` 을, restore 는 ``RestoreResult`` 를
돌려준다. 판정은 ``decide()`` 가 한다.

빈 목록(rc 0, ``[]``)은 그대로 ``RemoteEmpty`` 다. 0.5.17·0.5.18 은 복제본 경로·prefix 오타와
"복제본 없음"을 같은 출력으로 돌려주므로 이 모듈은 둘을 구분하지 못한다. 그 빈 목록을 새 DB 의
근거로 쓸지는 판정 정책이 정한다(``decide()`` 의 ``init_new``, D-13).

출력 형식·동작은 Litestream 0.5.17·0.5.18 에서 실측했다(``tests/fixtures/litestream-<판>/``).
검증하지 않은 버전이면 원격 조회와 restore 를 하지 않고 실패로 돌려준다.
"""

import json
import os
import re
import signal
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .decide import LookupFailure, Remote, RemoteEmpty, RemoteError, RemoteTxid

__all__ = [
    "DEFAULT_LTX_TIMEOUT",
    "DEFAULT_RESTORE_TIMEOUT",
    "DEFAULT_VERSION_TIMEOUT",
    "LtxSummary",
    "LtxWalRange",
    "VERIFIED_VERSIONS",
    "RestoreResult",
    "VersionProblem",
    "check_version",
    "config_databases",
    "default_meta_path",
    "local_max_ltx",
    "ltx_wal_range",
    "local_max_txid",
    "parse_databases_json",
    "parse_ltx_json",
    "parse_version",
    "remote_listing",
    "remote_max_txid",
    "restore",
    "summarize_ltx_json",
    "verified_versions_text",
    "version",
]

# 실측으로 출력 형식과 동작을 확인한 버전. 범위 밖이면 판정할 수 없으므로 거부한다.
# 판마다 tests/fixtures/litestream-<판>/ 의 실측 fixture 가 있어야 한다(테스트가 집합을 맞춘다).
VERIFIED_VERSIONS = frozenset({"0.5.17", "0.5.18"})

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
#   [48:56] WAL offset  [56:64] WAL size  [64:68] WAL salt-1  [68:72] WAL salt-2
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


def _run(argv: list[str], timeout: float, *, cwd: str | None = None) -> _Run | str:
    """명령을 실행한다. 실행하지 못했거나 시간이 넘으면 사유 문자열을 돌려준다."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
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


@dataclass(frozen=True, slots=True)
class VersionProblem:
    """버전 게이트 실패. ``message`` 는 사유 한 줄, ``failure`` 는 그 종류(#100).

    ``detail`` 은 ``VERSION_UNSUPPORTED`` 면 설치된 판, ``VERSION_PARSE`` 면 받은 출력 한 줄이고,
    ``VERSION_RUN`` 이면 빈 문자열이다.
    """

    failure: LookupFailure
    message: str
    detail: str = ""


def verified_versions_text() -> str:
    """검증한 판 목록 한 줄(``0.5.17, 0.5.18``)."""
    return ", ".join(sorted(VERIFIED_VERSIONS))


def check_version(
    *, binary: str = "litestream", timeout: float = DEFAULT_VERSION_TIMEOUT
) -> VersionProblem | None:
    """검증한 버전이면 ``None``, 아니면(읽지 못한 경우 포함) ``VersionProblem``."""
    run = _run([binary, "version"], timeout)
    if isinstance(run, str):
        return VersionProblem(LookupFailure.VERSION_RUN, run)
    if run.rc != 0:
        return VersionProblem(LookupFailure.VERSION_RUN, _failure("version", run))
    version = parse_version(run.stdout)
    if version is None:
        output = _one_line(run.stdout)
        return VersionProblem(
            LookupFailure.VERSION_PARSE,
            f"cannot parse litestream version output: {output!r}",
            output,
        )
    if version not in VERIFIED_VERSIONS:
        return VersionProblem(
            LookupFailure.VERSION_UNSUPPORTED,
            f"unsupported litestream version {version}; verified: {verified_versions_text()}",
            version,
        )
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


# 스냅샷 레벨(Litestream SnapshotLevel). 컴팩션된 끝을 정할 때 뺀다(summarize_ltx_json).
_SNAPSHOT_LEVEL = 9


@dataclass(frozen=True, slots=True)
class LtxSummary:
    """복제본 목록(``ltx -level all -json``)에서 본 컴팩션 진행(#115, DESIGN §7).

    ``compacted_txid`` 는 레벨 1~8 의 최대 ``max_txid``(사실상 L1 의 끝, 없으면 0)다. L1 파일은
    보존으로 지워질 때도 레벨의 마지막 파일이 남고(Litestream ``EnforceRetentionByTXID``), L2~L8 은
    L(n−1) 에서만 만들어지므로 L1 끝보다 클 수 없다. L9(스냅샷)는 DB 의 현재 위치에서 새로
    쓰이므로(``db.go`` ``WriteLTXFile(SnapshotLevel, 1, pos.TXID)``) 넣으면 컴팩션 정지를 가린다.
    Litestream 자신도 L0 보존에서 L1 의 최대 TXID 를 '컴팩션된 끝'으로 본다
    (``EnforceL0RetentionByTime``).

    ``uncompacted`` 는 ``min_txid`` 가 그 끝보다 큰 L0 의 수, 시각은 UNIX 초다. 한 복제본 안의
    시각은 같은 시계(S3 ``LastModified``·파일 mtime)라 서로 비교한다. 로컬 시계와는 비교하지
    않는다. ``problem`` 이 있으면 판정할 수 없다는 뜻이다(고정 문장).
    """

    l0_files: int
    compacted_txid: int
    uncompacted: int
    oldest_uncompacted_at: float | None
    newest_at: float | None
    problem: str | None = None

    @property
    def compaction_lag(self) -> float | None:
        """가장 새 객체 시각 − 가장 오래된 미컴팩션 L0 시각(초). 미컴팩션 L0 가 없으면 0.

        L0 는 계속 오는데 L1 이 오지 않으면 벽시계와 같은 속도로 는다. L0 기록까지 멈추면(새
        객체가 없음) 늘지 않는다 — 그 경우는 로컬 근거(``db_not_replicated``)가 맡는다.
        """
        if self.problem is not None:
            return None
        if self.uncompacted == 0 or self.oldest_uncompacted_at is None or self.newest_at is None:
            return 0.0
        return max(self.newest_at - self.oldest_uncompacted_at, 0.0)


def _parse_timestamp(value: object) -> float | None:
    """RFC 3339(``2006-01-02T15:04:05Z07:00``) → UNIX 초. 시간대가 없거나 읽을 수 없으면 None."""
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _bad_summary(problem: str) -> LtxSummary:
    return LtxSummary(0, 0, 0, None, None, problem)


def summarize_ltx_json(stdout: str) -> LtxSummary:
    """``litestream ltx -level all -json`` 의 stdout 을 컴팩션 요약으로 바꾼다(예외 없음).

    ``parse_ltx_json()`` 과 같은 출력을 읽지만 boot 판정에는 쓰지 않는다: boot 는 시각이 필요
    없고, 시각 형식이 조금 바뀌었다고 부팅을 막으면 안 된다. 항목 하나라도 시각을 읽을 수 없으면
    ``problem`` 이 있는 요약이다(헬스가 ``caught_up`` 이라 하지 않는다).
    """
    try:
        data = json.loads(_strip_log_lines(stdout))
    except ValueError:
        return _bad_summary("cannot parse litestream ltx output as JSON")
    if type(data) is not list:
        return _bad_summary("unexpected litestream ltx output: expected a list")
    l0: list[tuple[int, float]] = []
    compacted = 0
    newest: float | None = None
    for i, item in enumerate(data):
        if type(item) is not dict:
            return _bad_summary(f"unexpected litestream ltx output: item {i} is not an object")
        level, lo, hi = item.get("level"), item.get("min_txid"), item.get("max_txid")
        if type(level) is not int or not 0 <= level <= _SNAPSHOT_LEVEL:
            return _bad_summary(f"unexpected litestream ltx output: item {i} has invalid level")
        if type(lo) is not str or not _TXID_RE.fullmatch(lo):
            return _bad_summary(f"unexpected litestream ltx output: item {i} has invalid min_txid")
        if type(hi) is not str or not _TXID_RE.fullmatch(hi):
            return _bad_summary(f"unexpected litestream ltx output: item {i} has invalid max_txid")
        at = _parse_timestamp(item.get("timestamp"))
        if at is None:
            return _bad_summary(
                f"unexpected litestream ltx output: item {i} has no readable timestamp"
            )
        newest = at if newest is None else max(newest, at)
        if level == 0:
            l0.append((int(lo, 16), at))
        elif level < _SNAPSHOT_LEVEL:
            compacted = max(compacted, int(hi, 16))
    pending = [at for lo, at in l0 if lo > compacted]
    return LtxSummary(len(l0), compacted, len(pending), min(pending, default=None), newest)


def remote_listing(
    db_path: str | os.PathLike[str],
    *,
    config: str | os.PathLike[str],
    binary: str = "litestream",
    timeout: float = DEFAULT_LTX_TIMEOUT,
    version_timeout: float = DEFAULT_VERSION_TIMEOUT,
) -> tuple[Remote, LtxSummary | None]:
    """``remote_max_txid()`` 와 같은 조회 한 번으로 최대 TXID 와 컴팩션 요약을 함께 돌려준다.

    요약은 조회가 성공해 TXID 가 있을 때만 있다(실패·빈 목록이면 ``None``). 헬스가 쓴다(#115).
    """
    problem = check_version(binary=binary, timeout=version_timeout)
    if problem is not None:
        return RemoteError(problem.message, problem.failure, problem.detail), None
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
        return RemoteError(run), None
    if run.rc != 0:
        return RemoteError(_failure("ltx", run)), None
    remote = parse_ltx_json(run.stdout)
    if type(remote) is not RemoteTxid:
        return remote, None
    return remote, summarize_ltx_json(run.stdout)


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

    주의(0.5.17·0.5.18 실측): 복제본 경로가 없거나 비어 있어도 rc 0 과 ``[]`` 다. 둘은 출력으로
    구분되지 않으므로 둘 다 ``RemoteEmpty`` 다. 설정에 없는 DB·설정 파일 없음·접근 불가는 rc 1.
    """
    return remote_listing(
        db_path, config=config, binary=binary, timeout=timeout, version_timeout=version_timeout
    )[0]


# --- 설정의 DB 목록 -------------------------------------------------------------------


def parse_databases_json(stdout: str) -> list[str] | str:
    """``litestream databases -json`` 의 stdout 에서 DB 경로 목록을 읽는다. 실패면 사유 문자열.

    0.5.17·0.5.18 출력은 ``[{"path": "<절대 경로>", "replica": "<종류>"}, ...]`` 다. Litestream 은
    경로를 ``filepath.Abs`` 로 바꿔 내보낸다: 상대 경로는 Litestream 의 작업 디렉터리 기준이 되고
    ``..`` 는 문자열로 접힌다. 링크는 그대로 남는다(실측, ``tests/fixtures/litestream-<판>/``).
    """
    body = _strip_log_lines(stdout)
    try:
        data = json.loads(body)
    except ValueError as exc:
        return _one_line(f"cannot parse litestream databases output as JSON: {exc}")
    if type(data) is not list:
        return f"unexpected litestream databases output: expected a list, got {type(data).__name__}"
    paths = []
    for i, item in enumerate(data):
        if type(item) is not dict:
            return f"unexpected litestream databases output: item {i} is not an object"
        path = item.get("path")
        if type(path) is not str or not os.path.isabs(path) or "\0" in path:
            return f"unexpected litestream databases output: item {i} has invalid path"
        paths.append(path)
    return paths


def config_databases(
    config: str | os.PathLike[str],
    *,
    binary: str = "litestream",
    timeout: float = DEFAULT_LTX_TIMEOUT,
    version_timeout: float = DEFAULT_VERSION_TIMEOUT,
    cwd: str | None = None,
) -> list[str] | str:
    """설정 파일의 DB 경로 목록(``litestream databases -json``). 실패면 사유 문자열.

    YAML 을 직접 읽지 않는다. 경로 해석(환경 변수 확장·상대 경로·``..``)을 Litestream 이 하는
    그대로 본다. 상대 경로는 ``cwd`` 기준으로 풀린다. 설정 파일 없음·깨진 YAML 은 rc 1(실측).
    """
    problem = check_version(binary=binary, timeout=version_timeout)
    if problem is not None:
        return problem.message
    run = _run([binary, "databases", "-config", os.fspath(config), "-json"], timeout, cwd=cwd)
    if isinstance(run, str):
        return run
    if run.rc != 0:
        return _failure("databases", run)
    return parse_databases_json(run.stdout)


# --- 로컬 TXID ------------------------------------------------------------------------


def default_meta_path(db_path: str | os.PathLike[str]) -> Path:
    """Litestream 기본 메타 디렉터리 ``<dir>/.<name>-litestream`` (설정의 meta-path 가 없을 때)."""
    db = Path(db_path)
    return db.parent / f".{db.name}-litestream"


def _read_ltx_header(path: Path) -> bytes | None:
    """검증한 LTX 헤더 100바이트. 정규 파일·헤더 형식이 아니면 ``None``.

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
    return header


def _read_ltx_range(path: Path) -> tuple[int, int] | None:
    """LTX 파일 헤더의 (min TXID, max TXID). 정규 파일·헤더 형식이 아니면 ``None``."""
    header = _read_ltx_header(path)
    if header is None:
        return None
    return int.from_bytes(header[16:24], "big"), int.from_bytes(header[24:32], "big")


@dataclass(frozen=True, slots=True)
class LtxWalRange:
    """L0 파일이 원래 WAL 의 어디까지를 담았는가(superfly/ltx v0.5.2 ``Header``).

    ``offset + size`` 가 그 L0 가 읽은 WAL 의 끝(바이트)이다. Litestream 0.5.17 도 다음 동기화를
    이 위치와 salt 에서 이어 간다(``db.go`` ``verifyWithExecutor``: ``info.offset =
    WALOffset + WALSize``). 저널 모드나 압축 파일은 0 이다.
    """

    page_size: int
    offset: int
    size: int
    salt1: int
    salt2: int

    @property
    def end(self) -> int:
        return self.offset + self.size


def ltx_wal_range(path: str | os.PathLike[str]) -> LtxWalRange | None:
    """LTX 헤더의 WAL 위치 필드. 읽을 수 없거나 WAL 정보가 없으면(``offset`` 0) ``None``.

    필드(``ltx.go`` ``Header.MarshalBinary`` 293–296, big-endian): ``[48:56]`` WALOffset(int64),
    ``[56:64]`` WALSize(int64), ``[64:68]`` WALSalt1, ``[68:72]`` WALSalt2. ``Header.Validate``
    (228–241)처럼 음수 금지, salt 가 있으면 offset 이 0 이 아니어야 한다.
    """
    header = _read_ltx_header(Path(path))
    if header is None:
        return None
    offset = int.from_bytes(header[48:56], "big", signed=True)
    size = int.from_bytes(header[56:64], "big", signed=True)
    salt1 = int.from_bytes(header[64:68], "big")
    salt2 = int.from_bytes(header[68:72], "big")
    if offset <= 0 or size < 0:
        return None
    return LtxWalRange(int.from_bytes(header[8:12], "big"), offset, size, salt1, salt2)


def local_max_ltx(
    db_path: str | os.PathLike[str], *, meta_path: str | os.PathLike[str] | None = None
) -> tuple[int, Path] | None:
    """``local_max_txid()`` 와 같은 후보 하나를 같은 규칙으로 검증해 ``(max TXID, 파일 경로)``.

    헬스(#7)가 그 파일의 mtime 을 DB 변경 시각과 비교하려고 쓴다. 믿을 수 없으면 ``None``.
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
    return hi, l0 / name


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
    found = local_max_ltx(db_path, meta_path=meta_path)
    return None if found is None else found[0]


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
        return RestoreResult(False, problem.message)
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
