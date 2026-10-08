"""배포 프로필 문서의 조각으로 랩 빌드 컨텍스트(``lab/.build/``)를 만든다.

랩이 검증하는 대상은 문서 그 자체다. 그래서 Dockerfile·``requirements.txt``·``entrypoint.sh``·
``settings.py``·``urls.py``·``litestream.yml``·``compose.yaml`` 을 손으로 옮겨 적지 않고
``docs/profiles/*.md`` 의 코드 블록에서 꺼낸다. 바꾸는 곳은 아래 ``SUBSTITUTIONS`` 뿐이고,
바꿀 줄이 정확히 한 번 나오지 않으면 실패한다(문서가 바뀌면 여기서 드러난다).

    python lab/build_context.py <wheel>     # lab/.build/ 를 새로 만든다
"""

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT / "lab"
BUILD = LAB / ".build"
PROFILES = ROOT / "docs" / "profiles"

WHEEL_PLACEHOLDER = "<wheel>"

# (문서, 블록 고르기, 원문 줄, 바꾼 줄). 랩과 문서의 차이는 이 표가 전부다(lab/README.md).
SUBSTITUTIONS = {
    "Dockerfile": [
        # 작업 트리에서 만든 wheel 을 requirements 설치 전에 넣는다. 문서와 다른 단 한 줄.
        ("COPY requirements.txt .", f"COPY requirements.txt {WHEEL_PLACEHOLDER} ./"),
    ],
    "requirements.txt": [
        (
            "django-sqlite-ops @ https://github.com/itda-work/django-sqlite-ops/archive/refs/heads/main.zip",
            f"django-sqlite-ops @ file:///app/{WHEEL_PLACEHOLDER}",
        ),
    ],
    "litestream.yml": [
        ("      bucket: my-app-backups", "      bucket: dso-lab"),
        (
            "      path: app                    # 버킷 안 prefix. 오타가 나면 boot 가 '복제본 없음'과 구분하지 못한다",  # noqa: E501
            "      path: ${LAB_PREFIX}",
        ),
        ("      endpoint: http://s3.internal:8333", "      endpoint: http://toxiproxy:18333"),
    ],
}


def blocks(doc: str, lang: str) -> list[str]:
    text = (PROFILES / doc).read_text(encoding="utf-8")
    return re.findall(rf"^```{lang}\n(.*?)^```", text, flags=re.S | re.M)


def one(found: list[str], what: str) -> str:
    if len(found) != 1:
        raise SystemExit(f"expected exactly one {what} block, found {len(found)}")
    return found[0]


def substitute(name: str, text: str, wheel: str) -> str:
    lines = text.splitlines()
    for old, new in SUBSTITUTIONS.get(name, []):
        hits = [i for i, line in enumerate(lines) if line == old]
        if len(hits) != 1:
            raise SystemExit(f"{name}: expected the line {old!r} exactly once, found {len(hits)}")
        lines[hits[0]] = new.replace(WHEEL_PLACEHOLDER, wheel)
    return "\n".join(lines) + "\n"


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def main(wheel_path: str, build: Path = BUILD) -> None:
    """``build`` 를 새로 만든다. 테스트는 임시 디렉터리를 넘긴다."""
    wheel = Path(wheel_path)
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise SystemExit(f"not a wheel: {wheel}")

    single = "single-server.md"
    multi = "single-server-multiproc.md"
    dockerfile = one(blocks(single, "dockerfile"), "dockerfile")
    requirements = one(
        [b for b in blocks(single, "text") if "django-sqlite-ops @" in b], "requirements"
    )
    litestream = one(
        [b for b in blocks(single, "yaml") if b.startswith("# /etc/litestream.yml")], "litestream"
    )
    single_compose = one([b for b in blocks(single, "yaml") if "services:" in b], "single compose")
    multi_compose = one([b for b in blocks(multi, "yaml") if "services:" in b], "multi compose")
    single_entry = one(blocks(single, "bash"), "single entrypoint")
    multi_entry = one(blocks(multi, "bash"), "multiproc entrypoint")
    single_py = blocks(single, "python")
    single_settings = one([b for b in single_py if "SQLITE_OPS" in b], "single settings")
    urls = one([b for b in single_py if "urlpatterns" in b], "urls")
    multi_settings = one(
        [
            b
            for b in blocks(multi, "python")
            if "SQLITE_OPS" in b and "channels_nats" in b and "INSTALLED_APPS = [\n" in b
        ],
        "multiproc settings (channels-nats)",
    )

    # 디렉터리 하나만 지운다(CLAUDE.md 삭제 규칙). 경로는 상수다.
    if build.exists():
        shutil.rmtree(build)
    shutil.copytree(LAB / "app", build, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(wheel, build / wheel.name)

    write(build / "Dockerfile", substitute("Dockerfile", dockerfile, wheel.name))
    write(build / "requirements.txt", substitute("requirements.txt", requirements, wheel.name))
    lite = substitute("litestream.yml", litestream, wheel.name)
    write(build / "litestream.yml", lite)
    # L8b: 헬스의 원격 조회만 따로 끊을 수 있게 다른 toxiproxy 포트를 쓰는 설정.
    write(
        build / "lab_tools" / "litestream-health.yml",
        lite.replace("http://toxiproxy:18333", "http://toxiproxy:18334"),
    )
    write(build / "entrypoint.sh", single_entry, 0o755)
    write(build / "entrypoint-multiproc.sh", multi_entry, 0o755)

    tail = (LAB / "app" / "proj" / "settings_tail.py").read_text(encoding="utf-8")
    write(build / "proj" / "settings.py", single_settings + "\n" + tail)
    write(build / "proj" / "settings_multiproc.py", multi_settings + "\n" + tail)
    urls_tail = (LAB / "app" / "proj" / "urls_tail.py").read_text(encoding="utf-8")
    write(build / "proj" / "urls.py", urls + "\n" + urls_tail)
    for name in ("settings_tail.py", "urls_tail.py"):
        (build / "proj" / name).unlink()

    write(build / "compose.yaml", single_compose)
    write(build / "compose-multiproc.yaml", multi_compose)
    for name in ("lab-compose.yaml", "profile-override.yaml", "multiproc-override.yaml"):
        shutil.copy2(LAB / name, build / name)
    # 랩 전용 더미 자격 증명. SeaweedFS 는 인증 없이 띄운다.
    write(
        build / ".env.litestream",
        "LITESTREAM_ACCESS_KEY_ID=lab\nLITESTREAM_SECRET_ACCESS_KEY=lab\n",
    )
    print(build)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
