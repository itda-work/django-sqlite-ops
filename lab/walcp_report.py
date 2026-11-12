"""#106 원자료(``lab/.out/walcp-<run>-<test>.json``) → 결과 문서의 표(마크다운).

    uv run --no-project python lab/walcp_report.py lab/.out/walcp-*-replica.json ...

파일 이름 끝(``replica``·``plain``·``stall``·``l0``)으로 종류를 고른다. 값은 원자료 그대로 옮기고
단위만 바꾼다(MB = 10^6 바이트).
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def mb(v) -> str:
    return "-" if v is None else f"{v / 1e6:.1f}"


def ms(d: dict | None, key: str) -> str:
    if not d or d.get(key) is None:
        return "-"
    return f"{d[key]:.0f}"


def gap(r: dict) -> str:
    if "missing_rows" not in r:
        return f"복원 실패 {r.get('remote')} {r.get('restore_stderr', '')}"
    return f"{r['hole_rows']}/{r['tail_rows']}"


def replica(data: dict) -> list[str]:
    v = data["litestream_version"]
    out = [
        f"### Q1·Q2 Litestream {v} (부하 {data['duration_s']:.0f}초, {data['load']}, "
        f"재시작은 {data['kill_at_s']:.0f}초에 SIGTERM·{data['hold_s']:.0f}초 뒤 재기동)",
        "",
        "| ckpt | 방식 | 회 | 커밋 행 | 빠진 행 구멍/꼬리(caught_up 뒤) | "
        "빠진 행 구멍/꼬리(stop 뒤) | 빠진 id 구간 | "
        "integrity | caught_up 초 | salt 변화 | mxFrame 되감김 | nBackfill 증가 | 막힌 표본 | "
        "WAL 최대 MB | 정지 중 salt 변화 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(data["runs"], key=lambda r: (-r["ckpt"], r["mode"], r["rep"])):
        live, stop, w = r["replica_live"], r["replica_after_stop"], r["wal"]
        down = r.get("wal_while_down", {})
        out.append(
            f"| {r['ckpt']} | {r['mode']} | {r['rep']} | {r['rows_committed']} | "
            f"{gap(live)} | {gap(stop)} | "
            f"{live.get('missing_ranges', [])[:3] or '-'} | "
            f"{live.get('integrity')}/{stop.get('integrity')} | "
            f"{r['caught_up'].get('seconds')} | {w['salt_changes']} | {w['mx_drops']} | "
            f"{w['backfill_steps']} | {w['blocked_samples']}/{w['samples']} | "
            f"{mb(w['wal_max_bytes'])} | {down.get('salt_changes', '-')} |"
        )
    errs = collections.Counter()
    for r in data["runs"]:
        errs.update(r["load_errors"])
    out += ["", f"부하 오류(전체 실행 합): {dict(errs) or '없음'}"]
    return out


def plain(data: dict) -> list[str]:
    out = [
        f"### Q2 대조: Litestream 없음(`labapp-plain`, 부하 {data['duration_s']:.0f}초)",
        "",
        "| ckpt | 커밋 행 | salt 변화 | mxFrame 되감김 | nBackfill 증가 | 막힌 표본 | "
        "WAL 최대 MB | WAL 없는 표본 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in data["runs"]:
        w = r["wal"]
        out.append(
            f"| {r['ckpt']} | {r['rows_committed']} | {w['salt_changes']} | {w['mx_drops']} | "
            f"{w['backfill_steps']} | {w['blocked_samples']}/{w['samples']} | "
            f"{mb(w['wal_max_bytes'])} | {w['wal_missing_samples']} |"
        )
    return out


def stall(data: dict) -> list[str]:
    v = data["litestream_version"]
    out = [
        f"### Q3 Litestream {v} 정지 {data['stall_s']:.0f}초 ({data['load']})",
        "",
        "| 정지 방식 | ckpt | WAL 증가 MB/분 | 정지 중 WAL 최대 MB | 정지 중 salt 변화 | "
        "쓰기 p50/p95 ms 정지 전 | 쓰기 p50/p95 정지 끝 | "
        "읽기 p50/p95 정지 전 | 읽기 p50/p95 정지 끝 | "
        "정지 중 헬스 | 재개 뒤 caught_up 초 | 빠진 행 구멍/꼬리 | integrity |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in data["runs"]:
        b = r["latency_before"]["last"] or {}
        e = r["latency_during"]["last"] or {}
        statuses = collections.Counter(s for _, s, _ in r["health_during"])
        live = r["replica_live"]
        out.append(
            f"| {r['kind']} | {r['ckpt']} | {r['growth_mb_per_min']} | "
            f"{mb(r['wal_during']['wal_max_bytes'])} | {r['wal_during']['salt_changes']} | "
            f"{ms(b.get('write'), 'p50_ms')}/{ms(b.get('write'), 'p95_ms')} | "
            f"{ms(e.get('write'), 'p50_ms')}/{ms(e.get('write'), 'p95_ms')} | "
            f"{ms(b.get('read'), 'p50_ms')}/{ms(b.get('read'), 'p95_ms')} | "
            f"{ms(e.get('read'), 'p50_ms')}/{ms(e.get('read'), 'p95_ms')} | "
            f"{dict(statuses)} | {r['caught_up'].get('seconds')} | "
            f"{gap(live)} | {live.get('integrity')} |"
        )
    return out


def l0(data: dict) -> list[str]:
    out = [
        f"### Q4 Litestream {data['litestream_version']} {data.get('target', 'remote')} L0 하나 "
        f"손상 뒤 {data['minutes']:.0f}분",
        "",
        "| 경과 초 | 레벨별 객체 수 | 헬스 | 원격 조회(boot 와 같음) | 로컬 행 |",
        "|---|---|---|---|---|",
    ]
    for row in data["series"]:
        rem = row["remote"]
        out.append(
            f"| {row['t']:.0f} | {row['levels']} | {row['health'][0]}/{row['health'][1]} | "
            f"{rem.get('remote')} {rem.get('txid', rem.get('detail', ''))} | {row['local_rows']} |"
        )
    end = data["restore_end"]
    out += [
        "",
        f"끝의 복원: rc {end.get('restore_rc')}, 행 {end.get('rows')}, "
        f"integrity {end.get('integrity')}, stderr {end.get('restore_stderr')}",
        f"WARN/ERROR 줄 {data['warn_error_lines']}개. 처음: {data['warn_error_first'][:3]}",
    ]
    return out


KINDS = {"replica": replica, "plain": plain, "stall": stall, "l0": l0}


def main(paths: list[str]) -> None:
    for p in paths:
        kind = Path(p).stem.rsplit("-", 1)[1]
        data = json.loads(Path(p).read_text())
        print("\n".join(KINDS[kind](data)))
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
