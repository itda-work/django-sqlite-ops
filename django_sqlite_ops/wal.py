"""SQLite WAL 의 커밋 위치를 파일 읽기만으로 계산해 최신 L0 가 담은 위치와 비교한다 (DESIGN §7).

DB 연결을 열지 않는다. WAL 파일을 ``O_RDONLY`` 로 열어 헤더와 프레임 헤더만 읽는다.
표준 라이브러리만 쓴다.

형식(https://www.sqlite.org/fileformat2.html#walformat):
- WAL 헤더 32바이트(big-endian): ``[0:4]`` 매직(0x377f0682 → 체크섬 little-endian,
  0x377f0683 → big-endian), ``[4:8]`` 버전 3007000, ``[8:12]`` page size, ``[12:16]``
  checkpoint seq, ``[16:20]`` salt-1, ``[20:24]`` salt-2, ``[24:32]`` 앞 24바이트의 체크섬.
- 프레임 헤더 24바이트 + 페이지: ``[0:4]`` page no, ``[4:8]`` 커밋 프레임이면 커밋 뒤 DB 크기
  (페이지 수, 0 이 아님), ``[8:12]``·``[12:16]`` salt, ``[16:24]`` 누적 체크섬(이전 체크섬에서
  이어 프레임 헤더 앞 8바이트와 페이지 데이터로 계산).
- 프레임은 salt 가 헤더와 같고 누적 체크섬이 맞을 때만 유효하다. 첫 무효 프레임에서 WAL 이
  끝난 것으로 본다(SQLite 의 복구 규칙, Litestream 0.5.17 ``WALReader.readFrame`` 과 같음).

체크섬까지 검증한다. salt 만 보면 쓰는 중인(헤더는 쓰였고 페이지는 덜 쓰인) 프레임을 커밋으로
셀 수 있고, Litestream 이 보는 유효 범위와 어긋난다. 비용은 L0 끝 **뒤의** 프레임만, 첫 커밋
프레임까지만 읽어 줄인다(L0 끝 바로 앞 프레임의 체크섬 필드에서 이어 계산한다 — Litestream
``NewWALReaderWithOffset`` 과 같은 방식).
"""

import os
import stat
import struct
from dataclasses import dataclass

from .boot.litestream import LtxWalRange

__all__ = [
    "IN_SYNC",
    "NO_EVIDENCE",
    "PENDING",
    "SCAN_LIMIT",
    "WalEvidence",
    "WalHeader",
    "compare",
    "read_wal_header",
]

WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24
_MAGIC_LE = 0x377F0682
_MAGIC_BE = 0x377F0683
_VERSION = 3007000
_PAGE_SIZES = frozenset(1 << n for n in range(9, 17))
# 한 번에 읽는 최대 바이트. 이 안에서 커밋 프레임을 못 찾으면(거대한 트랜잭션이 쓰이는 중)
# 판정하지 않는다.
SCAN_LIMIT = 64 * 1024 * 1024

PENDING = "pending"  # 최신 L0 가 담지 않은 커밋이 WAL 에 있다
IN_SYNC = "in_sync"  # WAL 의 커밋이 모두 최신 L0 범위 안이다
NO_EVIDENCE = "none"  # WAL 로는 판정할 수 없다


@dataclass(frozen=True, slots=True)
class WalHeader:
    little_endian: bool
    page_size: int
    salt1: int
    salt2: int
    checksum: tuple[int, int]


@dataclass(frozen=True, slots=True)
class WalEvidence:
    """``state`` 는 ``PENDING``·``IN_SYNC``·``NO_EVIDENCE``. ``reason`` 은 고정 문장이다.

    ``ltx_end``·``commit_end`` 는 표시용 바이트 오프셋이다(``commit_end`` 는 PENDING 이면
    L0 뒤 첫 커밋 프레임의 끝, IN_SYNC 면 ``None``).
    """

    state: str
    reason: str
    ltx_end: int | None = None
    commit_end: int | None = None
    salt_match: bool | None = None
    key: object = None


def _checksum(little_endian: bool, s0: int, s1: int, data: bytes) -> tuple[int, int]:
    words = struct.unpack(f"{'<' if little_endian else '>'}{len(data) // 4}I", data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def _open(path: str) -> int | None:
    """정규 파일이면 읽기 전용 fd. 링크를 따라가지 않고 FIFO 에서 멈추지 않는다."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _pread(fd: int, size: int, offset: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = os.pread(fd, size - len(data), offset + len(data))
        if not chunk:
            break
        data += chunk
    return data


def _parse_header(raw: bytes) -> WalHeader | None:
    if len(raw) < WAL_HEADER_SIZE:
        return None
    magic, version, page_size, _seq, salt1, salt2, c1, c2 = struct.unpack(">8I", raw[:32])
    if magic not in (_MAGIC_LE, _MAGIC_BE) or version != _VERSION:
        return None
    if page_size not in _PAGE_SIZES:
        return None
    little = magic == _MAGIC_LE
    if _checksum(little, 0, 0, raw[:24]) != (c1, c2):
        return None  # 체크포인트 중 쓰다 만 헤더(Litestream readHeader 와 같은 처리)
    return WalHeader(little, page_size, salt1, salt2, (c1, c2))


def read_wal_header(path: str) -> WalHeader | None:
    fd = _open(path)
    if fd is None:
        return None
    try:
        return _parse_header(_pread(fd, WAL_HEADER_SIZE, 0))
    except OSError:
        return None
    finally:
        os.close(fd)


def _first_commit_after(
    fd: int, header: WalHeader, offset: int, seed: tuple[int, int], limit: int
) -> int | None | bool:
    """``offset`` 부터 유효 프레임을 따라가 첫 커밋 프레임의 끝 오프셋.

    유효 프레임이 끝날 때까지 커밋이 없으면 ``None``, ``limit`` 을 넘으면 ``False``.
    """
    frame = FRAME_HEADER_SIZE + header.page_size
    s0, s1 = seed
    start = offset
    while True:
        if offset - start + frame > limit:
            return False
        raw = _pread(fd, frame, offset)
        if len(raw) < frame:
            return None
        _pgno, commit, salt1, salt2, c1, c2 = struct.unpack(">6I", raw[:24])
        if (salt1, salt2) != (header.salt1, header.salt2):
            return None
        s0, s1 = _checksum(header.little_endian, s0, s1, raw[:8])
        s0, s1 = _checksum(header.little_endian, s0, s1, raw[24:])
        if (s0, s1) != (c1, c2):
            return None
        offset += frame
        if commit:
            return offset


def _snapshot(fd: int) -> tuple[int, int, int, bytes]:
    """처음 연 WAL 의 (st_dev, st_ino, 크기, 헤더 32바이트)."""
    st = os.fstat(fd)
    return st.st_dev, st.st_ino, st.st_size, _pread(fd, WAL_HEADER_SIZE, 0)


def _unchanged(fd: int, path: str, snap: tuple[int, int, int, bytes]) -> bool:
    """읽는 동안 WAL 이 바뀌지 않았는가: 같은 경로가 같은 파일(dev, ino)이고, 크기와 헤더
    (salt·체크섬 포함 32바이트)가 처음과 같다. 확인할 수 없으면 바뀐 것으로 본다."""
    try:
        st = os.fstat(fd)
        at_path = os.lstat(path)
        return (
            (st.st_dev, st.st_ino, st.st_size) == snap[:3]
            and (at_path.st_dev, at_path.st_ino) == snap[:2]
            and _pread(fd, WAL_HEADER_SIZE, 0) == snap[3]
        )
    except (OSError, ValueError):
        return False


def compare(wal_path: str, ltx: LtxWalRange | None, *, limit: int = SCAN_LIMIT) -> WalEvidence:
    """현재 WAL 에 최신 L0 가 담지 않은 커밋이 있는가.

    - 같은 salt: L0 끝(``WALOffset + WALSize``) 뒤에 유효한 커밋 프레임이 있으면 PENDING, 없으면
      IN_SYNC.
    - 다른 salt(L0 이후 WAL 이 다시 시작됨): 현재 salt 의 커밋 프레임이 있으면 PENDING(새 세대의
      커밋을 Litestream 이 아직 L0 로 쓰지 않았다), 없으면 NO_EVIDENCE(체크포인트·재시작 사이를
      Litestream 이 관측했는지 알 수 없다).
    - WAL 이 없거나 헤더를 읽을 수 없음, L0 에 WAL 정보가 없음, page size 불일치, L0 끝이
      프레임 경계가 아니거나 WAL 보다 김, 그 직전 프레임의 salt 가 다름 → NO_EVIDENCE.

    잠금 없이 읽으므로 읽는 사이에 체크포인트(TRUNCATE·RESTART)나 쓰기가 끼어들 수 있다.
    Litestream 은 체크포인트를 막는 읽기 트랜잭션을 쥐고 읽지만 헬스는 DB 연결을 열지 않는다.
    예: L0 끝 직전 프레임을 읽은 직후 TRUNCATE 가 WAL 을 0바이트로 만들면 꼬리 스캔은 EOF 를
    보고 이미 있던 미복제 커밋을 "없음"으로 읽는다(review-3 재현). 그래서 PENDING 이 아닌 결과를
    돌려주기 전에 처음 연 WAL 의 (dev, ino)·크기·헤더를 다시 확인하고, 바뀌었으면 한 번만 다시
    읽는다. 다시 읽어도 바뀌면 NO_EVIDENCE("the -wal changed during read"). PENDING 은 L0 뒤의
    유효한 커밋 프레임을 이미 읽었다는 근거이므로 그대로 돌려준다.
    """
    if ltx is None:
        return WalEvidence(NO_EVIDENCE, "the latest local L0 has no WAL position")
    for _attempt in range(2):
        result, stable = _compare_once(wal_path, ltx, limit)
        if stable:
            return result
    return WalEvidence(NO_EVIDENCE, "the -wal changed during read", ltx.end)


def _compare_once(wal_path: str, ltx: LtxWalRange, limit: int) -> tuple[WalEvidence, bool]:
    """한 번 읽는다. ``(결과, 믿을 수 있는가)``.

    PENDING 이거나 읽는 동안 WAL 이 그대로였으면 참이다.
    """
    fd = _open(wal_path)
    if fd is None:
        return WalEvidence(NO_EVIDENCE, "no readable -wal file", ltx.end), True
    try:
        try:
            snap = _snapshot(fd)
            result = _evaluate(fd, snap[3], ltx, limit)
        except OSError:
            return WalEvidence(NO_EVIDENCE, "the -wal could not be read", ltx.end), False
        if result.state == PENDING:
            return result, True
        return result, _unchanged(fd, wal_path, snap)
    finally:
        os.close(fd)


def _evaluate(fd: int, raw_header: bytes, ltx: LtxWalRange, limit: int) -> WalEvidence:
    header = _parse_header(raw_header)
    if header is None:
        return WalEvidence(NO_EVIDENCE, "the -wal header is empty or not valid", ltx.end)
    if header.page_size != ltx.page_size:
        return WalEvidence(NO_EVIDENCE, "the -wal page size differs from the L0", ltx.end)
    frame = FRAME_HEADER_SIZE + header.page_size
    same = (header.salt1, header.salt2) == (ltx.salt1, ltx.salt2)
    if not same:
        end = _first_commit_after(fd, header, WAL_HEADER_SIZE, header.checksum, limit)
        if end is False:
            return WalEvidence(NO_EVIDENCE, "the -wal scan limit was reached", ltx.end)
        if end is None:
            return WalEvidence(
                NO_EVIDENCE,
                "the -wal restarted after the latest L0 and has no commit yet",
                ltx.end,
                salt_match=False,
            )
        return WalEvidence(
            PENDING,
            "the -wal restarted after the latest L0 and has commits not in it",
            ltx.end,
            end,
            False,
        )
    if ltx.end == WAL_HEADER_SIZE:
        seed = header.checksum
    else:
        if (ltx.end - WAL_HEADER_SIZE) % frame or ltx.end < WAL_HEADER_SIZE + frame:
            return WalEvidence(NO_EVIDENCE, "the L0 WAL end is not on a frame boundary", ltx.end)
        if os.fstat(fd).st_size < ltx.end:
            return WalEvidence(NO_EVIDENCE, "the -wal is shorter than the L0 WAL end", ltx.end)
        prev = _pread(fd, FRAME_HEADER_SIZE, ltx.end - frame)
        if len(prev) < FRAME_HEADER_SIZE:
            return WalEvidence(NO_EVIDENCE, "the -wal is shorter than the L0 WAL end", ltx.end)
        _pgno, _commit, salt1, salt2, c1, c2 = struct.unpack(">6I", prev)
        if (salt1, salt2) != (header.salt1, header.salt2):
            return WalEvidence(
                NO_EVIDENCE, "the -wal frame before the L0 WAL end was overwritten", ltx.end
            )
        seed = (c1, c2)
    end = _first_commit_after(fd, header, ltx.end, seed, limit)
    if end is False:
        return WalEvidence(NO_EVIDENCE, "the -wal scan limit was reached", ltx.end)
    if end is None:
        return WalEvidence(
            IN_SYNC, "every -wal commit is within the latest L0", ltx.end, None, True
        )
    return WalEvidence(PENDING, "the -wal has commits after the latest L0", ltx.end, end, True)
