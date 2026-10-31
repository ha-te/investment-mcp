# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock .
# Optional combined CA mount for builds in a managed proxy environment.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir --require-hashes -r requirements.lock; \
    else pip install --no-cache-dir --require-hashes -r requirements.lock; fi
RUN groupadd --gid 10001 investment && useradd --uid 10001 --gid investment --no-create-home investment \
    && mkdir /data && chown investment:investment /data
COPY --chown=10001:10001 --chmod=644 *.py ./
COPY entrypoint.sh ./
RUN chmod 755 /app/entrypoint.sh
ENV INVESTMENT_DB=/data/investment.sqlite INVESTMENT_DATA_DIR=/data PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "remote_app.py"]
