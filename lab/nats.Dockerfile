# P2(#66): 문서 Dockerfile 로 만든 랩 이미지 위에 채널 레이어 패키지만 더한다.
# multiproc 문서 §3 "requirements.txt 에 채널 레이어 패키지를 더한다. 이 패키지의 [nats] extra
# 로 넣어도 된다" 의 extra 경로다. 문서 Dockerfile 은 그대로 두고 층 하나를 얹는다.
ARG BASE
FROM ${BASE}
USER root
COPY requirements-nats.txt ./
# 문서 이미지에는 pip 가 없고 패키지는 /opt/venv 에 uv 로 넣는다(#117).
RUN uv pip install -r requirements-nats.txt
USER app
