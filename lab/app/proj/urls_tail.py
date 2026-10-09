# --- lab 추가분 (lab/app/proj/urls_tail.py) -----------------------------------------------
# 위는 배포 프로필 문서의 urls.py 조각 그대로다. 아래는 시나리오·벤치용 뷰다.
from notes import views as lab_views  # noqa: E402
from notes.timing import timed  # noqa: E402

# 벤치 계측(#26): 뷰 본문 시간 헤더와 DB 요청 수. probe·ping 은 DB 를 열지 않는다.
urlpatterns += [  # noqa: F821
    path("lab/write", timed(lab_views.write)),  # noqa: F821
    path("lab/count", timed(lab_views.count)),  # noqa: F821
    path("lab/read/<int:pk>", timed(lab_views.read)),  # noqa: F821
    path("lab/sorted", timed(lab_views.sorted_page)),  # noqa: F821
    path("lab/stats", timed(lab_views.stats)),  # noqa: F821
    path("lab/probe", timed(lab_views.probe, db=False)),  # noqa: F821
    path("lab/ping", timed(lab_views.ping, db=False)),  # noqa: F821
    path("lab/soakprobe", timed(lab_views.soakprobe, db=False)),  # noqa: F821
    path("lab/soakenv", timed(lab_views.soakenv, db=False)),  # noqa: F821
]
