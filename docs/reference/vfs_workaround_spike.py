"""litestream VFS 확장을 프로세스에 한 번 등록한다.

VFS 는 sqlite3_vfs_register 로 프로세스 전역에 등록되므로, 아무 연결에서 한 번만
load_extension 하면 이후 Django 가 여는 연결은 URI 의 vfs=litestream 으로 쓸 수 있다.

우회(Linux, litestream-vfs 0.5.17): 확장이 auto-extension 도 등록하는데, VFS 가 아닌
일반 연결에서 실패 코드를 돌려 이후 모든 sqlite3.connect() 가
'automatic extension loading failed' 로 죽는다(상류 PR #1506, 미머지).
로드 직후 sqlite3_reset_auto_extension() 으로 auto-extension 만 해제한다. VFS 등록은 유지된다.
"""
import ctypes
import sqlite3
import _sqlite3
import litestream_vfs

_loader = sqlite3.connect(":memory:")
_loader.enable_load_extension(True)
_loader.load_extension(litestream_vfs.loadable_path(), entrypoint="sqlite3_litestreamvfs_init")
_loader.enable_load_extension(False)
try:
    ctypes.CDLL(_sqlite3.__file__).sqlite3_reset_auto_extension()
except (OSError, AttributeError):
    pass
# _loader 는 닫지 않는다(확장 언로드 방지 목적, 무해)
