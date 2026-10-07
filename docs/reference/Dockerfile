FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "django==6.1.*" gunicorn litestream-vfs==0.5.17 boto3
COPY litestream /usr/local/bin/litestream
COPY app /app
COPY entrypoint.sh guard.py /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh && rm -rf /app/*/__pycache__
WORKDIR /app
ENV DB_PATH=/data/app.sqlite3
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
