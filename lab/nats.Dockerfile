# P2(#66): 문서 Dockerfile 로 만든 랩 이미지 위에 채널 레이어 패키지만 더한다.
# multiproc 문서 §3 "requirements.txt 에 채널 레이어 패키지를 더한다. 이 패키지의 [nats] extra
# 로 넣어도 된다" 의 extra 경로다. 문서 Dockerfile 은 그대로 두고 층 하나를 얹는다.
ARG BASE
FROM ${BASE}
USER root
COPY requirements-nats.txt ./
RUN pip install --no-cache-dir -r requirements-nats.txt
USER app
