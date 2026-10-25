"""앱 컨테이너 안의 ``litestream ltx`` 프로세스를 나열한다(#65 L8c·L8d).

이미지에 ``ps`` 가 없으므로 ``/proc`` 를 직접 읽는다. 출력은 JSON 한 줄:
``{"ltx": [[pid, state, argv], ...]}``. 표준 라이브러리만 쓴다.
"""

import json
import os


def main() -> None:
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            with open(f"/proc/{name}/stat") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue  # 읽는 사이 끝났다
        # 좀비는 cmdline 이 비므로 comm 으로도 본다
        if not argv:
            try:
                with open(f"/proc/{name}/comm") as f:
                    comm = f.read().strip()
            except OSError:
                continue
            if comm == "litestream" and state == "Z":
                found.append([int(name), state, ["litestream", "?"]])
            continue
        if os.path.basename(argv[0]) == "litestream" and argv[1:2] == ["ltx"]:
            found.append([int(name), state, argv])
    print(json.dumps({"ltx": sorted(found)}))


if __name__ == "__main__":
    main()
