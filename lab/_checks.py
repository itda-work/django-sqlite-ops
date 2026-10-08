"""랩 시나리오의 판정을 순수 함수로 둔다. Docker 없이 ``tests/test_lab.py`` 가 검사한다.

- L6: kill 전 템플릿 볼륨의 격리 대상(DB·사이드카·메타 하위 전부)이 재실행 뒤 **같은 하나의**
  격리 디렉터리에 같은 해시로 있는지(``template_fingerprint``·``quarantine_problems``).
- L8a: S3 전체 단절 동안 헬스가 제한 시간 안에 ``unknown(stale)`` 로 바뀌고 복구 전까지
  유지되는지(``full_outage_problems``). 표본 인덱스가 아니라 경과 시간과 결과의 age 로 본다.

입력은 ``lab_tools/inspect_data.py`` 의 스냅샷(``{"entries": {상대경로: {...}}}``)과 헬스 표본이다.
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

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
