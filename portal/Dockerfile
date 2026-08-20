FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup -S portal && adduser -S -G portal portal

WORKDIR /opt/portal
COPY app /opt/portal/app
COPY content /opt/portal/content
COPY migrations /opt/portal/migrations

RUN mkdir -p /data && chown portal:portal /data

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2).read()"]

ENTRYPOINT ["python3", "-m", "app.entrypoint"]
CMD ["serve"]
