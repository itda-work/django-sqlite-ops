"""회귀 랩 시나리오 L1–L8 (DESIGN §10) 과 배포 프로필 종단 검증 P1·P2.

모든 시나리오는 자기 복제본 prefix(``<id>-<run>``)와 자기 볼륨을 쓴다. 앱 컨테이너는 한 번에
하나만 띄운다(쓰는 쪽은 언제나 하나). 실행: ``scripts/lab.sh run [L1..L8|P1|P2|all]``.
"""

from __future__ import annotations

import threading
import time

import pytest
from _checks import (
    full_outage_deadline,
    full_outage_problems,
    quarantine_problems,
    stale_dirs,
    template_fingerprint,
)
from _lab import Stack, boot_lines, decision, http_json, log

META = ".app.sqlite3-litestream"


class Volumes:
    """v01..v40 를 시나리오에 하나씩 나눠 준다. 이미 만들어진 볼륨(앞선 실행)은 건너뛴다."""

    _next = 1

    @classmethod
    def take(cls) -> str:
        while cls._next <= 40:
            name = f"v{cls._next:02d}"
            cls._next += 1
            if not Stack("main").volume_exists(name):
                return name
        raise RuntimeError("volume pool exhausted (lab-compose.yaml v01..v40)")


def env(volume: str | None, prefix: str, flags: str = "", **extra: str) -> dict[str, str]:
    e = {"LAB_PREFIX": prefix, "BOOT_FLAGS": flags, **extra}
    if volume:
        e["LAB_VOLUME"] = volume
    return e


def write_rows(port: int, n: int, *, batch: int = 1) -> int:
    done = 0
    while done < n:
        k = min(batch, n - done)
        http_json("POST", port, f"/lab/write?n={k}")
        done += k
    return done


def count(port: int) -> int:
    return http_json("GET", port, "/lab/count")["count"]


def wait_replica_rows(stack: Stack, prefix: str, rows: int, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = stack.replica(prefix)
        if last.get("rows") == rows:
            return last
        time.sleep(2)
    raise AssertionError(f"replica {prefix} did not reach {rows} rows: {last}")


def seed_volume(stack: Stack, volume: str, prefix: str, rows: int) -> None:
    """생애 첫 배포(--init-new)로 rows 행을 쓰고, 복제를 확인한 뒤 정상 종료한다."""
    port = stack.up_app(env=env(volume, prefix, "--init-new"))
    write_rows(port, rows, batch=10)
    wait_replica_rows(stack, prefix, rows)
    rc, _, _ = stack.stop_app()
    assert rc == 0


def db_entry(snapshot: dict) -> dict:
    return snapshot["entries"]["app.sqlite3"]


def key_lines(text: str, *needles: str) -> list[str]:
    lines = [ln for ln in text.splitlines() if any(n in ln for n in needles)]
    return [ln[:300] for ln in lines]


# --- 배포 프로필 종단 검증 ---------------------------------------------------------------------


def _profile_stop_check(stack: Stack, rec: dict, prefix: str, workers: int) -> None:
    # 이름 있는 빈 볼륨을 처음 붙일 때 이미지의 /data 소유자가 복사되는가
    owner = (
        stack.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "stat",
            "app",
            "-c",
            "%u:%g %a",
            "/data",
            env=env(None, prefix),
        )
        .out.strip()
        .splitlines()[-1]
    )
    rec["fresh_volume_data_owner"] = owner
    assert owner.startswith("10001:10001"), owner

    check = stack.compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "app",
        "manage.py",
        "check",
        "--deploy",
        env=env(None, prefix),
        check=False,
    )
    rec["check_deploy_sqlite_ops"] = key_lines(check.text, "sqlite_ops")
    assert "sqlite_ops" not in check.text

    t0 = time.monotonic()
    port = stack.up_app("app", env=env(None, prefix, "--init-new"))
    rec["first_boot_to_serving_s"] = round(time.monotonic() - t0, 1)

    pids = {http_json("GET", port, "/lab/count")["pid"] for _ in range(60)}
    rec["worker_pids_seen"] = len(pids)
    assert len(pids) == workers

    doctor = stack.compose(
        "exec",
        "-T",
        "app",
        "python",
        "manage.py",
        "sqlite_doctor",
        "--litestream-config",
        "/etc/litestream.yml",
        check=False,
    )
    rec["doctor_rc"] = doctor.rc
    rec["doctor_channels"] = key_lines(doctor.out, "backend")
    assert doctor.rc == 0, doctor.text

    write_rows(stack.host_port("app"), 400)
    # 쓰기 직후 곧바로 docker stop: 마지막 sync 가 400건을 모두 올리는가(D1a 의 ASGI 판)
    rc, took, logs = stack.stop_app("app")
    rec["stop_exit_code"] = rc
    rec["stop_seconds"] = round(took, 2)
    rec["shutdown_lines"] = key_lines(
        logs,
        "Shutting down",
        "Finished server process",
        "Stopping parent",
        "shut down",
        "signal",
        "exit",
    )
    assert rc == 0
    replica = stack.replica(prefix)
    rec["replica_after_stop"] = replica
    assert replica.get("rows") == 400 and replica.get("integrity") == "ok"

    # 같은 볼륨으로 다시: match → 진행
    port = stack.up_app("app", env=env(None, prefix))
    assert count(port) == 400
    rec["reboot_decision"] = decision(stack.logs("app"))
    assert "state=match" in rec["reboot_decision"]
    stack.stop_app("app")


@pytest.mark.profile
def test_P1_single_server_compose(run_id, rec):
    """single-server 문서의 Dockerfile·compose·entrypoint 그대로(uvicorn 워커 1)."""
    stack = Stack("p1")
    try:
        stack.up_infra()
        _profile_stop_check(stack, rec, f"p1-{run_id}", workers=1)
    finally:
        stack.down()


@pytest.mark.profile
def test_P2_multiproc_compose(run_id, rec):
    """single-server-multiproc 문서의 compose·entrypoint(uvicorn 워커 2, nats 컨테이너)."""
    stack = Stack("p2", env={"WEB_WORKERS": "2"})
    try:
        stack.up_infra()
        _profile_stop_check(stack, rec, f"p2-{run_id}", workers=2)
    finally:
        stack.down()


# --- L1–L8 -------------------------------------------------------------------------------


def test_L1_fresh_container_restores(stack, run_id, rec):
    """볼륨 없이 새 컨테이너 → fresh → 복원, 행 수 일치(무상태 교체 D1c 포함)."""
    prefix = f"l1-{run_id}"
    seed_volume(stack, Volumes.take(), prefix, 200)

    t0 = time.monotonic()
    port = stack.up_app("labapp-nv", env=env(None, prefix))
    rec["boot_to_serving_s"] = round(time.monotonic() - t0, 1)
    logs = stack.logs("labapp-nv")
    rec["decision"] = decision(logs)
    assert "state=fresh action=restore" in rec["decision"]
    assert count(port) == 200
    rec["rows"] = 200
    # 무상태 컨테이너도 이어서 쓰고 복제한다
    write_rows(port, 10)
    rc, took, _ = stack.stop_app("labapp-nv")
    assert rc == 0
    rec["replica_after_stop"] = wait_replica_rows(stack, prefix, 210)


def test_L2_old_volume_refused(stack, run_id, rec):
    """옛 볼륨으로 재부팅(D4) → unknown(remote_ahead) → exit 2, 복제본 손상 0."""
    prefix = f"l2-{run_id}"
    va, vb = Volumes.take(), Volumes.take()
    seed_volume(stack, va, prefix, 50)
    port = stack.up_app(env=env(vb, prefix))  # B 는 복제본에서 복원해 이어 쓴다
    write_rows(port, 100)
    wait_replica_rows(stack, prefix, 150)
    assert stack.stop_app()[0] == 0
    before_replica = stack.replica(prefix)
    before_a = stack.inspect_volume(va)

    res = stack.run_boot(env=env(va, prefix))
    rec["exit_code"] = res.rc
    rec["boot_lines"] = boot_lines(res.text)
    assert res.rc == 2
    assert "reason_code=remote_ahead" in decision(res.text)

    after_replica = stack.replica(prefix)
    rec["replica_before_after"] = [before_replica, after_replica]
    assert after_replica["txid"] == before_replica["txid"]
    assert after_replica["rows"] == 150
    after_a = stack.inspect_volume(va)
    assert db_entry(after_a)["sha256"] == db_entry(before_a)["sha256"]
    assert db_entry(after_a)["rows"] == 50


def test_L3_meta_deleted_refused(stack, run_id, rec):
    """로컬 메타만 삭제 → unknown(no_local_meta) → 거부. DB 는 그대로."""
    prefix = f"l3-{run_id}"
    v = Volumes.take()
    seed_volume(stack, v, prefix, 30)
    stack.remove_meta(v)
    before = stack.inspect_volume(v)
    assert META not in before["entries"]

    res = stack.run_boot(env=env(v, prefix))
    rec["exit_code"] = res.rc
    rec["boot_lines"] = boot_lines(res.text)
    assert res.rc == 2
    assert "reason_code=no_local_meta" in decision(res.text)
    after = stack.inspect_volume(v)
    assert db_entry(after)["sha256"] == db_entry(before)["sha256"]
    assert db_entry(after)["rows"] == 30


def test_L4_s3_down_at_boot_refused(stack, run_id, rec):
    """S3 끊김 중 부팅 → unknown(remote_error) → 거부. 끊김(연결 거부)과 무응답 둘 다."""
    prefix = f"l4-{run_id}"
    v = Volumes.take()
    seed_volume(stack, v, prefix, 20)
    before = stack.inspect_volume(v)
    try:
        stack.proxy_enabled("s3", False)
        t0 = time.monotonic()
        res = stack.run_boot(env=env(v, prefix))
        rec["refused_exit_code"] = res.rc
        rec["refused_seconds"] = round(time.monotonic() - t0, 1)
        rec["refused_boot_lines"] = boot_lines(res.text)
        assert res.rc == 2
        assert "reason_code=remote_error" in decision(res.text)
        stack.proxy_enabled("s3", True)

        # 무응답: 연결은 받고 데이터를 보내지 않는다. boot 의 --ltx-timeout 이 끊는다.
        stack.add_toxic("s3", {"name": "hang", "type": "timeout", "attributes": {"timeout": 0}})
        t0 = time.monotonic()
        res = stack.run_boot(env=env(v, prefix, "--ltx-timeout 5"))
        rec["hang_exit_code"] = res.rc
        rec["hang_seconds"] = round(time.monotonic() - t0, 1)
        rec["hang_boot_lines"] = boot_lines(res.text)
        assert res.rc == 2
        assert "reason_code=remote_error" in decision(res.text)
    finally:
        stack.reset_proxies()
    after = stack.inspect_volume(v)
    assert db_entry(after)["sha256"] == db_entry(before)["sha256"]
    # 복구 뒤에는 그대로 진행한다
    port = stack.up_app(env=env(v, prefix))
    assert count(port) == 20
    assert "state=match" in decision(stack.logs("labapp"))
    stack.stop_app()


def test_L5_unreplicated_commits_kept(stack, run_id, rec):
    """복제되지 않은 로컬 커밋이 있는 채로 재부팅 → match → 진행, 커밋 보존."""
    prefix = f"l5-{run_id}"
    v = Volumes.take()
    port = stack.up_app(env=env(v, prefix, "--init-new"))
    write_rows(port, 40)
    wait_replica_rows(stack, prefix, 40)
    try:
        stack.proxy_enabled("s3", False)
        write_rows(port, 25)
        time.sleep(3)  # Litestream 이 로컬 L0 를 쓰도록(sync-interval 1s)
        stack.kill_app()  # SIGKILL: 마지막 sync 없이
    finally:
        stack.proxy_enabled("s3", True)
    rec["replica_before_reboot"] = stack.replica(prefix)
    assert rec["replica_before_reboot"]["rows"] == 40

    port = stack.up_app(env=env(v, prefix))
    logs = stack.logs("labapp")
    rec["decision"] = decision(logs)
    rec["boot_lines"] = boot_lines(logs)
    assert "state=match" in rec["decision"] and "reason_code=local_current" in rec["decision"]
    assert count(port) == 65
    rec["replica_after"] = wait_replica_rows(stack, prefix, 65)
    stack.stop_app()


def _rename_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("[lab-hook] rename")]


def test_L6_kill_during_restore(stack, run_id, rec):
    """``--on-unknown restore`` 의 격리·설치 도중 k 번째 rename 직후 SIGKILL → 재실행으로 완료.

    rename 은 순식간이라 kill 시점을 맞추려고 lab_hooks/sitecustomize.py 가 rename 뒤마다
    잠든다(패키지 코드는 그대로). 매 kill 지점마다 같은 '옛 볼륨'(remote_ahead)을 복사해 쓴다.
    """
    prefix = f"l6-{run_id}"
    template, vb = Volumes.take(), Volumes.take()
    seed_volume(stack, template, prefix, 50)
    port = stack.up_app(env=env(vb, prefix))
    write_rows(port, 100)
    wait_replica_rows(stack, prefix, 150)
    assert stack.stop_app()[0] == 0

    hook = {"LAB_PYTHONPATH": "/app/lab_hooks"}
    flags = "--on-unknown restore"
    # kill 전 템플릿의 격리 대상(DB·사이드카·메타 하위 전부)과 해시
    fingerprint = template_fingerprint(stack.inspect_volume(template))
    rec["template_fingerprint"] = fingerprint

    # 먼저 끝까지 돌려 rename 횟수(=kill 지점)를 센다
    probe = Volumes.take()
    stack.copy_volume(template, probe)
    stack.up_app(env=env(probe, prefix, flags, LAB_RENAME_DELAY="0.01", **hook))
    probe_logs = stack.logs("labapp")
    boot_part = probe_logs.split("[boot] exec:")[0]
    renames = _rename_lines(boot_part)
    rec["renames"] = renames
    stack.stop_app()
    assert len(renames) >= 4  # manifest, db, meta, partial→final, install, state ...
    # 설치(복원본 → DB 경로) rename 뒤에 죽으면 D-14 경로로 한 번 더 격리된다
    install = next(i for i, ln in enumerate(renames, 1) if ".restore-" in ln.split(" -> ")[0])

    points = []
    for k in range(1, len(renames) + 1):
        v = Volumes.take()
        stack.copy_volume(template, v)
        stack.start_app(env=env(v, prefix, flags, LAB_RENAME_DELAY="5", **hook))
        deadline = time.monotonic() + 60
        while len(_rename_lines(stack.logs("labapp"))) < k:
            assert time.monotonic() < deadline, stack.logs("labapp")
            time.sleep(0.2)
        stack.kill_app()
        # kill 이 정말 k 번째 rename 뒤·k+1 번째 전이었는지 로그로 확인한다(sleep 에 기대지 않는다)
        renamed_before_kill = len(_rename_lines(stack.logs("labapp")))
        killed = stack.inspect_volume(v)
        partials = [e for e in killed["entries"] if e.endswith(".partial") and "/" not in e]

        # 재실행(훅 없이). 남은 상태에 따라 재개·fresh 복원·D-14 재격리 중 하나로 끝나야 한다.
        port = stack.up_app(env=env(v, prefix, flags))
        logs = stack.logs("labapp")
        rows = count(port)
        stack.stop_app()
        final = stack.inspect_volume(v)
        expected_stales = 2 if k >= install else 1
        problems = quarantine_problems(final, fingerprint, expected_stales)
        point = {
            "k": k,
            "after": renames[k - 1].split(": ", 1)[1].replace("/data/", ""),
            "renames_before_kill": renamed_before_kill,
            "partials_after_kill": partials,
            "rerun_decision": decision(logs),
            "rerun_rows": rows,
            "stale_dirs": stale_dirs(final),
            "expected_stales": expected_stales,
            "problems": problems,
        }
        points.append(point)
        log(f"L6 k={k}: {point}")
        assert renamed_before_kill == k, point
        assert rows == 150, point
        assert not problems, point
    rec["points"] = points


def test_L7_corrupt_replica_restore_fails(stack, run_id, rec):
    """복제본 LTX 객체 손상 → 복원 실패 exit 4, 로컬 무변경."""
    prefix = f"l7-{run_id}"
    v = Volumes.take()
    seed_volume(stack, v, prefix, 60)
    corrupted = stack.s3_objects(prefix, "corrupt").out.strip().splitlines()[-1]
    rec["corrupted"] = corrupted
    assert '"applied": true' in corrupted

    # (a) 볼륨 없는 새 컨테이너: fresh → RESTORE 가 실패
    res = stack.run_boot("labapp-nv", env=env(None, prefix))
    rec["fresh_exit_code"] = res.rc
    rec["fresh_boot_lines"] = boot_lines(res.text)
    assert res.rc == 4

    # (b) 로컬 DB 가 있는 볼륨(메타 삭제 → no_local_meta) + --on-unknown restore:
    #     복원이 먼저 실패하므로 격리하지 않는다.
    stack.remove_meta(v)
    before = stack.inspect_volume(v)
    res = stack.run_boot(env=env(v, prefix, "--on-unknown restore"))
    rec["quarantine_exit_code"] = res.rc
    rec["quarantine_boot_lines"] = boot_lines(res.text)
    assert res.rc == 4
    after = stack.inspect_volume(v)
    rec["left_for_investigation"] = [
        k for k in after["entries"] if ".restore-" in k and "/" not in k
    ]
    assert db_entry(after)["sha256"] == db_entry(before)["sha256"]
    assert not stale_dirs(after)
    same = {
        k: e.get("sha256") for k, e in before["entries"].items() if not k.endswith((".boot.lock",))
    }
    for k, sha in same.items():
        assert after["entries"][k].get("sha256") == sha, k


class Writer(threading.Thread):
    """L8 동안 꾸준히 쓴다. 오류 수를 센다."""

    def __init__(self, port: int, interval: float = 0.2):
        super().__init__(daemon=True)
        self.port, self.interval = port, interval
        self.ok = self.errors = 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                http_json("POST", self.port, "/lab/write?n=1", timeout=5)
                self.ok += 1
            except Exception:
                self.errors += 1
            time.sleep(self.interval)


def _health(port: int) -> tuple[str, str, float | None]:
    body = http_json("GET", port, "/internal/sqlite-health")
    d = body["databases"]["default"]
    return d["status"], d["code"], d["age"]


def _wait_health(port: int, want: str, timeout: float) -> list:
    seen = []
    deadline = time.monotonic() + timeout
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        st = _health(port)
        seen.append((round(time.monotonic() - t0, 1), *st))
        if st[0] == want:
            return seen
        time.sleep(1)
    raise AssertionError(f"health never {want}: {seen[-10:]}")


def _l8(stack: Stack, run_id: str, rec: dict, label: str, health_config: str) -> None:
    prefix = f"{label}-{run_id}"
    v = Volumes.take()
    e = env(
        v,
        prefix,
        "--init-new",
        LAB_HEALTH_REFRESH="2",
        LAB_HEALTH_GRACE="10",
        LAB_HEALTH_CONFIG=health_config,
    )
    port = stack.up_app(env=e)
    write_rows(port, 20)
    rec["before"] = _wait_health(port, "caught_up", 60)[-1]
    writer = Writer(port)
    writer.start()
    timeline = []
    try:
        stack.proxy_enabled("s3", False)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20:
            timeline.append((round(time.monotonic() - t0, 1), *_health(port)))
            time.sleep(1)
    finally:
        stack.proxy_enabled("s3", True)
    t1 = time.monotonic()
    after = _wait_health(port, "caught_up", 120)
    rec["recovered_after_s"] = round(time.monotonic() - t1, 1)
    writer.stop.set()
    writer.join()
    rec["outage_timeline"] = timeline
    rec["recovery_timeline"] = after
    rec["writes_ok_errors"] = (writer.ok, writer.errors)
    rec["outage_statuses"] = sorted({(s, c) for _, s, c, _a in timeline})
    final = count(port)
    stack.stop_app()
    rec["replica_after_stop"] = wait_replica_rows(stack, prefix, final)
    assert writer.errors == 0


def test_L8a_health_full_s3_outage(stack, run_id, rec):
    """S3 20초 끊김(업로드와 헬스 조회 모두) → caught_up 아님 → 복구 후 caught_up.

    헬스의 원격 조회도 끊기므로 §7 표대로 ``unknown`` 이 기대값이다.
    조회가 ``ltx`` 타임아웃(30초)까지 매달리면 결과가 오래되어 ``stale``,
    타임아웃이 끝나면 ``remote_error`` 다.
    """
    _l8(stack, run_id, rec, "l8a", "")
    # 마지막 관측의 age 가 REFRESH × 3 을 넘으면 stale 이다. 단절 직전에 끝난 조회가 있으면
    # 전환이 그만큼 늦으므로 경과 시간 기준의 제한(_checks.full_outage_deadline)으로 본다.
    rec["stale_deadline_s"] = full_outage_deadline(2)
    problems = full_outage_problems(rec["outage_timeline"], refresh=2)
    rec["problems"] = problems
    assert not problems, problems


def test_L8b_health_upload_outage(stack, run_id, rec):
    """업로드 경로만 20초 끊김(헬스 조회는 다른 프록시) → backlog → 복구 후 caught_up."""
    _l8(stack, run_id, rec, "l8b", "/app/lab_tools/litestream-health.yml")
    assert ("backlog", "local_ahead") in rec["outage_statuses"], rec["outage_statuses"]
