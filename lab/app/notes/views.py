"""랩 시나리오·벤치용 뷰. 동기 뷰다(Django 의 ASGI 핸들러가 스레드로 돌린다)."""

import os
import random

from django.conf import settings
from django.db import connection
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import metrics
from .models import Note

_BODY = "x" * 200


@csrf_exempt
def write(request):
    """POST: 행 ``n`` 개(기본 1)를 한 트랜잭션으로 쓴다."""
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    n = int(request.GET.get("n", "1"))
    notes = Note.objects.bulk_create(
        [Note(body=_BODY, score=random.randrange(1_000_000)) for _ in range(n)]
    )
    return JsonResponse({"written": len(notes), "last_id": notes[-1].pk if notes else None})


def count(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*), coalesce(max(id), 0) FROM notes_note")
        rows, max_id = cursor.fetchone()
    return JsonResponse({"count": rows, "max_id": max_id, "pid": os.getpid()})


def read(request, pk):
    note = Note.objects.filter(pk=pk).values("id", "score", "body").first()
    if note is None:
        raise Http404
    return JsonResponse({"id": note["id"], "score": note["score"], "len": len(note["body"])})


def sorted_page(request):
    """인덱스 없는 열로 정렬해 임시 B-트리를 쓰게 한다(temp_store 대상)."""
    lo = random.randrange(1_000_000)
    ids = list(
        Note.objects.filter(score__gte=lo, score__lt=lo + 20_000)
        .order_by("body", "-created")
        .values_list("id", flat=True)[:20]
    )
    return JsonResponse({"ids": ids})


def stats(request):
    name = settings.DATABASES["default"]["NAME"]
    sizes = {}
    for suffix in ("", "-wal", "-shm"):
        try:
            sizes[f"db{suffix}"] = os.stat(name + suffix).st_size
        except FileNotFoundError:
            sizes[f"db{suffix}"] = None
    pragmas = {}
    with connection.cursor() as cursor:
        for p in (
            "journal_mode",
            "synchronous",
            "busy_timeout",
            "temp_store",
            "mmap_size",
            "cache_size",
            "journal_size_limit",
        ):
            cursor.execute(f"PRAGMA {p}")
            pragmas[p] = cursor.fetchone()[0]
    return JsonResponse(
        {
            "sizes": sizes,
            "pragmas": pragmas,
            "conn_max_age": settings.DATABASES["default"].get("CONN_MAX_AGE", 0),
            "pid": os.getpid(),
        }
    )


def probe(request):
    """벤치 표본(#26): 카운터·WAL 크기와 헤더·CPU 시간. DB 연결을 열지 않는다."""
    return JsonResponse(metrics.snapshot())


def ping(request):
    """벤치(#26): DB 없이 Django 를 지나는 가장 가벼운 요청(부하 발생기·스택 한계 확인)."""
    return JsonResponse({"ok": True})


def soakprobe(request):
    """soak 표본(#33): fd 분류·RSS·스레드·cgroup 메모리·WAL 크기. DB 연결을 열지 않는다."""
    return JsonResponse(metrics.soak_snapshot())


def soakenv(request):
    """soak 환경(#33): nr_open·적용된 한도 원문. DB 연결을 열지 않는다."""
    return JsonResponse(metrics.soak_env())
