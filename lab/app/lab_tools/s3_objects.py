"""SeaweedFS filer HTTP API 로 복제본 객체를 나열하거나 손상시킨다(L7).

    python s3_objects.py <prefix> list
    python s3_objects.py <prefix> corrupt      # 모든 .ltx 객체의 본문 가운데 바이트를 뒤집는다

S3 버킷 ``dso-lab`` 은 filer 의 ``/buckets/dso-lab/`` 이다. 헤더(앞 100바이트)는 건드리지 않고
페이지 영역을 바꾼다. 결과는 JSON 한 줄.
"""

import json
import sys
import urllib.request

FILER = "http://s3:8888"
BUCKET = "dso-lab"
_DIR_BIT = 1 << 31


def _get(url: str, *, accept_json: bool = False) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json"} if accept_json else {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def walk(path: str) -> list[dict]:
    found = []
    data = json.loads(_get(f"{FILER}{path}?limit=10000", accept_json=True))
    for e in data.get("Entries") or []:
        if e["Mode"] & _DIR_BIT:
            found += walk(e["FullPath"] + "/")
        else:
            found.append({"path": e["FullPath"], "size": e["FileSize"]})
    return found


def corrupt(path: str) -> dict:
    body = bytearray(_get(FILER + path))
    if len(body) <= 200:
        return {"path": path, "skipped": "too small"}
    start = 100 + (len(body) - 100) // 2
    for i in range(start, min(start + 64, len(body))):
        body[i] ^= 0xFF
    req = urllib.request.Request(FILER + path, data=bytes(body), method="PUT")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    # 다시 읽어 바꾼 내용이 실제로 저장됐는지 본다.
    return {"path": path, "size": len(body), "applied": _get(FILER + path) == bytes(body)}


def main() -> None:
    prefix, action = sys.argv[1], sys.argv[2]
    objects = walk(f"/buckets/{BUCKET}/{prefix}/")
    if action == "list":
        print(json.dumps({"objects": objects}))
    elif action == "corrupt":
        results = [corrupt(o["path"]) for o in objects if o["path"].endswith(".ltx")]
        print(json.dumps({"corrupted": results}))
    else:
        raise SystemExit(f"unknown action {action}")


if __name__ == "__main__":
    main()
