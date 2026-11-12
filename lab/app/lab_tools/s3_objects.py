"""SeaweedFS filer HTTP API 로 복제본 객체를 나열하거나 손상시킨다(L7).

    python s3_objects.py <prefix> list
    python s3_objects.py <prefix> corrupt      # 모든 .ltx 객체의 본문 가운데 바이트를 뒤집는다
    python s3_objects.py <prefix> levels       # 레벨별 LTX 객체 수·바이트(#106)
    python s3_objects.py <prefix> corrupt-path <filer 경로>   # 객체 하나만 손상(#106)

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
    elif action == "levels":
        # #106 Q4: 레벨별 LTX 객체 수·바이트, 레벨마다 가장 앞·뒤 이름. 경로는
        # <prefix>/<레벨 4자리>/<min>-<max>.ltx 다(0.5.18 에서 확인, 0000 = L0, 0009 = 스냅샷).
        levels: dict = {}
        for o in objects:
            parts = o["path"].split("/")
            if not o["path"].endswith(".ltx") or len(parts) < 2 or not parts[-2].isdigit():
                continue
            level = str(int(parts[-2]))
            d = levels.setdefault(level, {"count": 0, "bytes": 0, "names": []})
            d["count"] += 1
            d["bytes"] += o["size"]
            d["names"].append(parts[-1])
        for d in levels.values():
            names = sorted(d.pop("names"))
            d["first"], d["last"] = names[0], names[-1]
        print(json.dumps({"levels": levels}))
    elif action == "corrupt-path":
        # #106 Q4: 객체 하나만 손상시킨다(경로는 levels/list 가 낸 이름으로 고른다)
        print(json.dumps({"corrupted": [corrupt(sys.argv[3])]}))
    else:
        raise SystemExit(f"unknown action {action}")


if __name__ == "__main__":
    main()
