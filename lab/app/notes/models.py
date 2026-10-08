from django.db import models


class Note(models.Model):
    body = models.TextField()
    score = models.IntegerField(db_index=True)
    created = models.DateTimeField(auto_now_add=True)
