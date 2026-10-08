import random

from django.core.management.base import BaseCommand
from django.db import transaction

from notes.models import Note


class Command(BaseCommand):
    help = "벤치용 초기 행을 쓴다(난수 시드 고정)."

    def add_arguments(self, parser):
        parser.add_argument("rows", type=int)
        parser.add_argument("--batch", type=int, default=5000)

    def handle(self, rows, batch, **options):
        rng = random.Random(10)
        body = "y" * 200
        done = 0
        while done < rows:
            n = min(batch, rows - done)
            with transaction.atomic():
                Note.objects.bulk_create(
                    [Note(body=body, score=rng.randrange(1_000_000)) for _ in range(n)]
                )
            done += n
        self.stdout.write(f"seeded {done}")
