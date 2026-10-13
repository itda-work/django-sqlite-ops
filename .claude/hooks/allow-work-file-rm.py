#!/usr/bin/env python3
"""PermissionRequest(Bash) 훅: `.work/issue-<N>/` 아래 파일을 지우는 단독 `rm` 만 확인 없이 허용한다.

전역 설정의 `ask: Bash(rm:*)` 때문에 구현 에이전트가 자기가 만든 작업 파일을 지울 때마다
멈춘다. 허용 규칙(allow)과 PreToolUse 훅의 allow 는 ask 규칙을 넘지 못한다(공식 문서, 실측 확인).
권한 확인 직전에 도는 PermissionRequest 훅만 그 확인을 대신 승인할 수 있어서 여기서 좁게 허용한다.

허용하는 꼴은 하나뿐이다:

    rm -f -- .work/issue-<N>/<이름> [.work/issue-<N>/<이름> ...]

- 명령 전체가 이 꼴이어야 한다(`;`·`&&`·리다이렉트·치환이 섞이면 허용하지 않는다).
- 경로는 저장소 기준 상대 경로, 와일드카드·`..`·하위 디렉터리 없음.
- 각 경로가 지금 정규 파일이어야 한다(디렉터리·심볼릭 링크·없음이면 허용하지 않는다).

조건에 맞지 않으면 아무것도 출력하지 않고 끝난다. 그러면 평소처럼 확인 창이 뜬다.
"""

import json
import os
import re
import sys

_PATH = r"\.work/issue-[0-9]+/[A-Za-z0-9._-]+"
_COMMAND = re.compile(rf"rm -f -- ({_PATH}(?: {_PATH})*)")


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    if data.get("tool_name") != "Bash":
        return
    command = (data.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return
    match = _COMMAND.fullmatch(command.strip())
    if match is None:
        return
    root = data.get("cwd") or os.getcwd()
    for rel in match.group(1).split(" "):
        name = rel.rsplit("/", 1)[1]
        if name in (".", ".."):
            return
        path = os.path.join(root, rel)
        if os.path.islink(path) or not os.path.isfile(path):
            return
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
