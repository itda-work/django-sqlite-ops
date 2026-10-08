"""L6 전용 훅: ``LAB_RENAME_DELAY`` 가 있으면 ``os.rename`` 뒤마다 한 줄 남기고 잠든다.

boot 의 격리·설치는 rename 몇 번이라 순식간에 끝난다. 하네스는 이 줄을 세어 k 번째 rename
직후에 컨테이너를 ``docker kill`` 한다. 패키지 코드는 바꾸지 않는다(``PYTHONPATH`` 로만 켠다).
"""

import os
import sys
import time

_delay = os.environ.get("LAB_RENAME_DELAY")
if _delay:
    _real_rename = os.rename
    _count = 0

    def _slow_rename(src, dst, *args, **kwargs):
        global _count
        _real_rename(src, dst, *args, **kwargs)
        _count += 1
        print(f"[lab-hook] rename {_count}: {src} -> {dst}", file=sys.stderr, flush=True)
        time.sleep(float(_delay))

    os.rename = _slow_rename
