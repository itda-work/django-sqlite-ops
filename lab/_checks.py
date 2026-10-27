"""랩 시나리오의 판정을 순수 함수로 둔다. Docker 없이 ``tests/test_lab.py`` 가 검사한다.

- L6: kill 전 템플릿 볼륨의 격리 대상(DB·사이드카·메타 하위 전부)이 재실행 뒤 **같은 하나의**
  격리 디렉터리에 같은 해시로 있는지(``template_fingerprint``·``quarantine_problems``).
- L8a: S3 전체 단절 동안 헬스가 제한 시간 안에 ``unknown(stale)`` 로 바뀌고 복구 전까지
  유지되는지(``full_outage_problems``). 표본 인덱스가 아니라 경과 시간과 결과의 age 로 본다.
- P2(#66): 워커 사이 채널 레이어 전달의 손실·중복·용량 드롭(``delivery_summary``·
  ``delivery_problems``). 입력은 ``lab_tools/chanlab_run.py`` 의 시도 원자료다.

입력은 ``lab_tools/inspect_data.py`` 의 스냅샷(``{"entries": {상대경로: {...}}}``)과 헬스 표본이다.
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import re

DB = "app.sqlite3"
META = ".app.sqlite3-litestream"
SIDECARS = ("-wal", "-shm", "-journal")
MANIFEST = "manifest.json"
STALE_PREFIX = f"{DB}.stale-"
# health.py STALE_FACTOR: 마지막 관측이 REFRESH × 3 보다 오래되면 stale
STALE_FACTOR = 3
# 응답의 age 는 소수 셋째 자리로 반올림된다(health.py). 판정은 반올림 전 값으로 하므로
# age 를 문턱과 비교할 때 반올림 오차만큼 허용한다(예: 6.0004초 → stale, 응답 age 6.0).
AGE_ROUNDING = 0.0005


def _is_target(rel: str) -> bool:
    return (
        rel == DB or rel in {DB + s for s in SIDECARS} or rel == META or rel.startswith(META + "/")
    )


def template_fingerprint(snapshot: dict) -> dict[str, str]:
    """격리 대상의 목록과 내용: ``{상대경로: sha256 또는 "dir"}``. DB 는 반드시 있어야 한다."""
    fp = {}
    for rel, e in snapshot["entries"].items():
        if not _is_target(rel):
            continue
        if e["type"] == "dir":
            fp[rel] = "dir"
        elif e["type"] == "file":
            fp[rel] = e["sha256"]
        else:
            raise ValueError(f"unexpected {e['type']} in template: {rel}")
    if DB not in fp:
        raise ValueError("template has no db file")
    return fp


def _fingerprint_of(entry: dict | None) -> str | None:
    if entry is None:
        return None
    if entry["type"] == "dir":
        return "dir"
    return entry.get("sha256")


def stale_dirs(snapshot: dict) -> list[str]:
    return sorted(
        rel
        for rel, e in snapshot["entries"].items()
        if "/" not in rel
        and rel.startswith(STALE_PREFIX)
        and not rel.endswith(".partial")
        and e["type"] == "dir"
    )


def quarantine_problems(
    snapshot: dict, fingerprint: dict[str, str], expected_stales: int
) -> list[str]:
    """재실행 뒤 볼륨이 기대 격리 구성인지 본다.

    문제를 사람용 문장 목록으로 돌려준다(빈 목록 = 통과).

    - ``.partial`` 이 남지 않았다.
    - 템플릿의 모든 대상이 한 격리 디렉터리 S 에 같은 종류·해시로 있고,
      S 에는 그것과 manifest 만 있다.
    - 템플릿 파일과 같은 해시의 파일이 S 밖 어디에도 없다(갈라짐·사본 없음).
    - 격리 디렉터리 수가 ``expected_stales`` 다. 2 이면(D-14: 설치 뒤 exec 전 kill) 나머지 하나는
      설치됐던 복원본만 담는다: manifest 와 DB 는 있고, 메타는 없으며, 그 밖에는 DB 사이드카만.
    """
    entries = snapshot["entries"]
    problems = []
    partials = [r for r in entries if "/" not in r and r.endswith(".partial")]
    if partials:
        problems.append(f"leftover .partial: {partials}")

    stales = stale_dirs(snapshot)
    if len(stales) != expected_stales:
        problems.append(f"expected {expected_stales} stale dir(s), found {stales}")

    def matches(s: str) -> int:
        return sum(
            _fingerprint_of(entries.get(f"{s}/{rel}")) == fp for rel, fp in fingerprint.items()
        )

    if not stales:
        return problems + ["no stale dir holds the template"]
    old = max(stales, key=matches)
    for rel, fp in sorted(fingerprint.items()):
        got = _fingerprint_of(entries.get(f"{old}/{rel}"))
        if got is None:
            problems.append(f"{old}/{rel} missing")
        elif got != fp:
            problems.append(f"{old}/{rel} differs from the template")
    allowed = set(fingerprint) | {MANIFEST}
    extra = sorted(
        r[len(old) + 1 :]
        for r in entries
        if r.startswith(old + "/") and r[len(old) + 1 :] not in allowed
    )
    if extra:
        problems.append(f"{old} has unexpected entries: {extra}")

    template_files = {fp for fp in fingerprint.values() if fp != "dir"}
    elsewhere = sorted(
        r
        for r, e in entries.items()
        if not r.startswith(old + "/") and e["type"] == "file" and e.get("sha256") in template_files
    )
    if elsewhere:
        problems.append(f"template content outside {old}: {elsewhere}")

    for other in (s for s in stales if s != old):
        names = {r[len(other) + 1 :] for r in entries if r.startswith(other + "/")}
        if expected_stales != 2:
            continue
        if not {MANIFEST, DB} <= names:
            problems.append(f"{other} (D-14 re-quarantine) lacks manifest or db: {sorted(names)}")
        if any(n == META or n.startswith(META + "/") for n in names):
            problems.append(f"{other} (D-14 re-quarantine) unexpectedly holds the meta dir")
        unexpected = names - {MANIFEST, DB, DB + "-wal", DB + "-shm"}
        if unexpected:
            problems.append(
                f"{other} (D-14 re-quarantine) has unexpected entries: {sorted(unexpected)}"
            )
    return problems


def full_outage_deadline(refresh: float, sample_interval: float = 1.0) -> float:
    """단절 시작부터 ``unknown(stale)`` 이 보여야 하는 마지막 시각(초).

    **랩의 허용 시간(가정)**이다.

    stale 문턱 ``REFRESH × 3`` + 단절 직전에 시작해 단절 직후 끝난 조회 하나의 여유(REFRESH 로
    가정) + 표본 간격 + 스케줄 여유 1초. health.py 는 조회 하나가 끝나는 시간을 제한하지 않고
    (REFRESH 는 조회가 끝난 뒤 기다리는 간격이다) 관측 시각은 조회가 끝날 때 찍히므로, 이 값은
    조회 시간에서 도출된 보장 상한이 아니다. 단절 직전 조회가 REFRESH 보다 오래 걸려 성공하면 이
    제한을 넘길 수 있다(그때는 L8a 가 실패로 알린다).
    """
    return refresh * STALE_FACTOR + refresh + sample_interval + 1.0


def full_outage_problems(timeline: list, refresh: float, sample_interval: float = 1.0) -> list[str]:
    """L8a 판정. ``timeline`` 은 ``(단절 뒤 초, status, code, age)`` 표본.

    - 제한 시간(``full_outage_deadline``) 안에 ``unknown``(``stale`` 또는
      ``remote_error``)이 처음 나온다.
    - 그 뒤 단절이 끝날 때까지 모든 표본이 그 상태를 유지한다.
    - 전환 전의 ``caught_up`` 은 결과의 age 가 stale 문턱 이하일 때만 정상이다.
    - ``stale`` 표본의 age 는 문턱을 넘는다.

    age 비교는 응답의 반올림(``AGE_ROUNDING``)만큼 허용한다: ``caught_up`` 은 age ≤ 문턱 + 오차,
    ``stale`` 은 age ≥ 문턱 − 오차.
    """
    problems = []
    limit = refresh * STALE_FACTOR
    down = {("unknown", "stale"), ("unknown", "remote_error")}
    first = next((i for i, (_, s, c, _a) in enumerate(timeline) if (s, c) in down), None)
    if first is None:
        return [f"never became unknown(stale|remote_error): {timeline}"]
    t_first = timeline[first][0]
    deadline = full_outage_deadline(refresh, sample_interval)
    if t_first > deadline:
        problems.append(f"became unknown at {t_first}s, later than {deadline}s")
    for t, s, _c, age in timeline[:first]:
        if s == "caught_up" and (age is None or age > limit + AGE_ROUNDING):
            problems.append(f"caught_up at {t}s with age {age} > {limit}")
    for t, s, c, age in timeline[first:]:
        if (s, c) not in down:
            problems.append(f"left unknown during the outage at {t}s: {s}/{c}")
        if c == "stale" and (age is None or age < limit - AGE_ROUNDING):
            problems.append(f"stale at {t}s with age {age} < {limit}")
    return problems


# --- L8c·L8d: 조회 타임아웃보다 긴 단절(#65) -------------------------------------------------
# boot/litestream.py: DEFAULT_LTX_TIMEOUT(헬스의 원격 조회 타임아웃, HEALTH 설정 키 없음)과
# 시간 초과 뒤 SIGKILL 한 프로세스를 거두는 여유 _KILL_GRACE.
LTX_TIMEOUT = 30.0
KILL_GRACE = 5.0


def long_outage_bounds(
    refresh: float, ltx_timeout: float = LTX_TIMEOUT, sample_interval: float = 1.0
) -> dict[str, float]:
    """L8c·L8d 의 시간 경계(초). 모두 **랩의 허용 시간(가정)**이다(보장 상한 아님).

    - ``stale_by``: L8a 와 같다(``full_outage_deadline``).
    - ``first_error_after``: 단절 시점에 진행 중이던 조회는 정상 조회 시간(1초 미만으로 가정) 안에
      시작했으므로 ``ltx_timeout − 1`` 전에는 타임아웃으로 끝나지 않는다.
    - ``first_error_by``: 단절 직후 성공으로 끝난 조회의 여유(REFRESH 로 가정) + 다음 조회까지의
      REFRESH + 그 조회의 타임아웃 + 표본 간격 + 1초.
    - ``period``: ``remote_error`` 관측 뒤 REFRESH 를 쉬고 시작한 조회가 다시 타임아웃으로
      끝나기까지. ``period_slack`` 은 표본 간격 + 1초.
    """
    return {
        "stale_by": full_outage_deadline(refresh, sample_interval),
        "first_error_after": ltx_timeout - 1.0,
        "first_error_by": refresh + refresh + ltx_timeout + sample_interval + 1.0,
        "period": refresh + ltx_timeout,
        "period_slack": sample_interval + 1.0,
    }


def error_episodes(timeline: list) -> list[float]:
    """``remote_error`` 구간이 시작한 표본 시각들(바로 앞 표본이 ``remote_error`` 가 아닌 것)."""
    starts, prev = [], None
    for t, _s, c, *_ in timeline:
        if c == "remote_error" and prev != "remote_error":
            starts.append(t)
        prev = c
    return starts


def long_outage_problems(
    timeline: list,
    refresh: float,
    outage: float,
    ltx_timeout: float = LTX_TIMEOUT,
    sample_interval: float = 1.0,
) -> list[str]:
    """L8c·L8d 판정. ``timeline`` 은 ``(단절 뒤 초, status, code, age, strict HTTP 코드)`` 표본.

    - L8a 의 판정(``full_outage_problems``)을 그대로 통과한다: 제한 시간 안에 ``unknown``, 단절
      끝까지 유지, ``caught_up``·``stale`` 의 age.
    - 첫 ``remote_error`` 는 ``[first_error_after, first_error_by]`` 안에 시작한다.
    - 그 뒤 ``remote_error`` 와 ``stale`` 이 번갈아 나온다: 구간 시작 사이 간격이
      ``period ± period_slack`` 이고, 단절 길이에서 기대되는 수 이상이다.
    - ``remote_error`` 표본의 age 는 stale 문턱 이하다(반올림 허용). health.py 의 판정 순서
      (age > 문턱이면 오류보다 ``stale`` 이 먼저)상 지금은 늘 참이다. 그 순서가 바뀌는 회귀를
      잡는 안전망이다.
    - strict 응답: ``unknown`` 은 503, ``caught_up`` 은 200.
    """
    problems = full_outage_problems([row[:4] for row in timeline], refresh, sample_interval)
    b = long_outage_bounds(refresh, ltx_timeout, sample_interval)
    limit = refresh * STALE_FACTOR
    starts = error_episodes(timeline)
    if not starts:
        problems.append("never became remote_error")
    else:
        if not b["first_error_after"] <= starts[0] <= b["first_error_by"]:
            problems.append(
                f"first remote_error at {starts[0]}s, outside "
                f"[{b['first_error_after']}, {b['first_error_by']}]s"
            )
        for a, z in zip(starts, starts[1:], strict=False):
            if abs((z - a) - b["period"]) > b["period_slack"]:
                problems.append(
                    f"remote_error episodes {a}s -> {z}s: gap {round(z - a, 1)}s, "
                    f"expected {b['period']}±{b['period_slack']}s"
                )
        want = 1 + int(max(0.0, outage - b["first_error_by"]) // (b["period"] + b["period_slack"]))
        if len(starts) < want:
            problems.append(f"{len(starts)} remote_error episode(s), expected >= {want}: {starts}")
    for t, s, c, age, http in timeline:
        if c == "remote_error" and (age is None or age > limit + AGE_ROUNDING):
            problems.append(f"remote_error at {t}s with age {age} > {limit}")
        want_http = 200 if s == "caught_up" else 503
        if http != want_http:
            problems.append(f"strict HTTP {http} at {t}s for {s}/{c}, expected {want_http}")
    return problems


def max_sample_gap(timeline: list) -> float:
    """표본 시각 사이의 가장 긴 간격(초). 표본이 둘 미만이면 0."""
    times = [row[0] for row in timeline]
    return max((b - a for a, b in zip(times, times[1:], strict=False)), default=0.0)


# 이보다 거친 헬스 표본은 시각 판정(구간 시작·간격)을 믿을 수 없다(하네스 문제, 판정 실패와 구분).
COARSE_SAMPLE_GAP = 1.5


def ltx_process_problems(
    outage: list,
    after: list,
    ltx_timeout: float = LTX_TIMEOUT,
    sample_slack: float = 5.0,
    min_pids: int = 2,
    min_busy_samples: int = 3,
) -> list[str]:
    """앱 컨테이너 안 ``litestream ltx`` 프로세스 표본 판정.

    ``outage``·``after`` 는 ``(초, [(pid, state), ...])`` 목록(단절 중 / 복구 뒤).

    - 검출기가 고장 나 빈 표본만 쌓여도 통과하지 않도록, 단절 중 ``ltx`` 를 본 표본이
      ``min_busy_samples`` 개 이상, 서로 다른 PID 가 ``min_pids`` 개 이상이어야 한다(90초 단절에서
      30초 조회가 2~3번 돈다).
    - 어느 표본에서도 2개 이상 동시에 있지 않다(갱신 스레드 하나가 순차 조회).
    - 좀비(``Z``)가 없다(시간 초과 뒤 거둔다).
    - 같은 PID 가 ``ltx_timeout + KILL_GRACE + sample_slack`` 보다 오래 보이지 않는다.
    - 단절 중 본 PID 가 복구 뒤 표본에 남지 않는다.
    """
    problems = []
    span: dict[int, list[float]] = {}
    for phase, samples in (("outage", outage), ("after", after)):
        for t, procs in samples:
            if len(procs) > 1:
                problems.append(f"{len(procs)} ltx processes at {phase} {t}s: {procs}")
            for pid, state in procs:
                if state == "Z":
                    problems.append(f"zombie ltx pid {pid} at {phase} {t}s")
                if phase == "outage":
                    span.setdefault(pid, [t, t])[1] = t
    busy = sum(1 for _t, procs in outage if procs)
    if busy < min_busy_samples:
        problems.append(f"ltx seen in {busy} outage sample(s), expected >= {min_busy_samples}")
    if len(span) < min_pids:
        problems.append(
            f"{len(span)} distinct ltx pid(s) during the outage, expected >= {min_pids}"
        )
    bound = ltx_timeout + KILL_GRACE + sample_slack
    for pid, (first, last) in sorted(span.items()):
        if last - first > bound:
            problems.append(f"ltx pid {pid} seen for {round(last - first, 1)}s > {bound}s")
    left = sorted({pid for _t, procs in after for pid, _s in procs} & set(span))
    if left:
        problems.append(f"ltx pids from the outage still running after recovery: {left}")
    return problems


# --- P2 채널 레이어 전달(#66) -------------------------------------------------------------

# channels-nats 0.10.0 의 용량 드롭 경고(layer.py ``_enqueue``). 첫 건과 그 뒤 60초마다
# "is full … N message(s) dropped so far", 자리가 나면 "has room again after dropping N".
_FULL = re.compile(r"mailbox for (\S+) is full \(capacity \d+\); (\d+) message\(s\) dropped so far")
_ROOM = re.compile(r"mailbox for (\S+) has room again after dropping (\d+) message\(s\)")


# nats-py ``Client._read_loop`` 이 일반 예외에서 남기고 읽기 루프를 끝내는 줄. 랩 뷰는
# 예외를 ``[종류: 메시지]`` 로 덧붙인다(notes/chanlab.py ``_Capture``). channels-nats 0.10.0
# 이하에서는 구독 확인이 취소되면 ``InvalidStateError`` 로 이 줄이 났다(#66·#88).
NATS_READ_ERROR = "nats: encountered error"


def _nats_lines(worker: dict) -> list[str]:
    """수신 중 로그 + stop 뒤에 늦게 온 로그. 취소된 확인의 PONG 은 stop 뒤에 올 수 있어
    ``cancel`` 모드면 chanlab_run 이 1초 뒤 다시 물어 ``late_nats_log`` 를 붙인다."""
    return worker["nats_log"] + worker.get("late_nats_log", [])


def nats_drops(lines: list[str], channel: str) -> int:
    """``channel`` 의 mailbox 가 버린 메시지 수. 드롭 구간마다 마지막 누적 값을 더한다."""
    total = current = 0
    for line in lines:
        if (m := _FULL.search(line)) and m[1] == channel:
            current = int(m[2])
        elif (m := _ROOM.search(line)) and m[1] == channel:
            total += int(m[2])
            current = 0
    return total + current


def delivery_summary(spec: dict, trial: dict, near: float = 0.05) -> dict:
    """시도 하나의 워커별 수신 요약. ``near`` 는 송신 창 앞뒤로 취소를 세는 여유(초)."""
    n = spec["n"]
    want = set(range(n))
    workers = []
    for w in trial["workers"]:
        ids = w["ids"]
        got = set(ids)
        workers.append(
            {
                "pid": w["pid"],
                "received": len(ids),
                "dup": len(ids) - len(got),
                "unknown": sorted(got - want)[:20],
                "lost": len(want - got),
                "in_order": ids == sorted(ids),
                "drops": nats_drops(w["nats_log"], w["channel"]),
                "cancels": w["cancels"],
                "cancels_near_send": sum(
                    1 for t in w["cancel_times"] if trial["t0"] - near <= t <= trial["t1"] + near
                ),
                "errors": w["errors"],
                "nats_errors": [m for m in _nats_lines(w) if NATS_READ_ERROR in m],
                "read_loop_alive": w.get("read_loop_alive"),
            }
        )
    every = [i for w in trial["workers"] for i in w["ids"]]
    union = set(every)
    return {
        "workers": workers,
        "total": len(every),
        "cross_dup": len(every) - len(union),
        "union_lost": len(want - union),
        "lost_ids": sorted(want - union)[:50],
        "send_errors": trial["errors"],
        "senders": trial["senders"],
    }


def delivery_problems(spec: dict, trial: dict) -> list[str]:
    """계약에 맞지 않는 것을 문장으로(빈 목록 = 통과). ``spec["expect"]``:

    - ``each``: 워커마다 0..n-1 을 정확히 한 번씩, 보낸 순서로(그룹·프로세스 전용 채널).
    - ``split``: 일반 채널 큐 그룹. 워커 합이 0..n-1 을 정확히 한 번씩, 두 워커 모두 1건 이상.
    - ``lossy``: 일반 채널 취소 공백. 손실은 허용, 중복·모르는 id·용량 드롭은 없음.
    - ``drops``: 버스트. 워커마다 손실 = 그 mailbox 의 용량 드롭 수, 중복 없음.
    - ``record``: 판정 대상(D2 재현). 중복·모르는 id 만 본다.

    모든 경우에 송신 예외·수신 루프 예외가 없어야 하고, 워커 수가 ``spec["workers"]``(기본 2)다.
    워커 ``nats_log`` 에 nats-py 읽기 루프 오류(``nats: encountered error``)가 없고, 그 워커가 쓴
    레이어 인스턴스의 읽기 루프가 끝나지 않았어야 한다(``read_loop_alive`` 가 False 가 아님) — 끝난
    연결은 아무것도 읽지 않는데(#66) ``lossy``·``record`` 는 손실로만 남기 때문이다. 메시지에 예외
    이름을 드러낸다. 준비되지 않은 시도(``ready: false``)도 실패다.
    """
    s = delivery_summary(spec, trial)
    n = spec["n"]
    expect = spec["expect"]
    problems = []
    if trial.get("ready") is False:
        problems.append(f"not ready after {trial.get('probes_sent')} probes")
    if len(s["workers"]) != spec.get("workers", 2):
        problems.append(f"{len(s['workers'])} workers reported")
    if s["send_errors"]:
        problems.append(f"send errors: {s['send_errors'][:3]}")
    for w in s["workers"]:
        tag = f"pid {w['pid']}"
        if w["errors"]:
            problems.append(f"{tag}: receive errors {w['errors'][:3]}")
        if w["nats_errors"]:
            problems.append(f"{tag}: nats-py read loop error {w['nats_errors'][:3]}")
        if w["read_loop_alive"] is False:
            problems.append(f"{tag}: nats-py read loop stopped")
        if w["unknown"]:
            problems.append(f"{tag}: unknown ids {w['unknown']}")
        if w["dup"]:
            problems.append(f"{tag}: {w['dup']} duplicate(s)")
        if expect == "each":
            if w["received"] != n or w["lost"]:
                problems.append(f"{tag}: received {w['received']}/{n}, lost {w['lost']}")
            if not w["in_order"]:
                problems.append(f"{tag}: out of order")
        elif expect == "drops" and w["lost"] != w["drops"]:
            problems.append(f"{tag}: lost {w['lost']} != mailbox drops {w['drops']}")
        elif expect in ("split", "lossy") and w["drops"]:
            problems.append(f"{tag}: {w['drops']} mailbox drop(s)")
    if expect in ("split", "lossy", "record"):
        if s["cross_dup"] - sum(w["dup"] for w in s["workers"]):
            problems.append("a message reached more than one worker")
        if s["total"] > n:
            problems.append(f"received {s['total']} > sent {n}")
    owner = {w["channel"]: w["pid"] for w in trial["workers"]}
    for job in trial.get("jobs", []):
        if owner.get(job["to"]) == job["sender"]:
            problems.append(f"worker {job['sender']} sent to its own channel")
    if expect == "split":
        if s["union_lost"]:
            problems.append(f"lost {s['union_lost']}: {s['lost_ids'][:10]}")
        if any(w["received"] == 0 for w in s["workers"]):
            problems.append("a worker got no share of the queue group")
    return problems
