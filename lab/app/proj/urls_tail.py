# --- lab 추가분 (lab/app/proj/urls_tail.py) -----------------------------------------------
# 위는 배포 프로필 문서의 urls.py 조각 그대로다. 아래는 시나리오·벤치용 뷰다.
from notes import views as lab_views  # noqa: E402

urlpatterns += [  # noqa: F821
    path("lab/write", lab_views.write),  # noqa: F821
    path("lab/count", lab_views.count),  # noqa: F821
    path("lab/read/<int:pk>", lab_views.read),  # noqa: F821
    path("lab/sorted", lab_views.sorted_page),  # noqa: F821
    path("lab/stats", lab_views.stats),  # noqa: F821
]
