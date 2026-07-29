FROM python:3.12.13-alpine3.24

ARG PIP_VERSION=26.1.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/flowdesk

COPY apps/cloud_worker/requirements.txt /tmp/flowdesk-worker-requirements.txt
RUN python -m pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}" \
    && python -m pip install --no-cache-dir -r /tmp/flowdesk-worker-requirements.txt \
    && python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
    && python -c 'import importlib.util; assert importlib.util.find_spec("pip") is None' \
    && addgroup -S -g 10001 flowdesk \
    && adduser -S -D -H -u 10001 -G flowdesk -s /sbin/nologin flowdesk \
    && install -d -o 10001 -g 10001 -m 0700 /scratch

COPY apps/cloud_worker /opt/flowdesk/apps/cloud_worker
COPY apps/connectors/databento/src /opt/flowdesk/apps/connectors/databento/src
COPY apps/market_service /opt/flowdesk/apps/market_service

USER 10001:10001

ENTRYPOINT ["python", "-m", "apps.cloud_worker"]
CMD ["--help"]
