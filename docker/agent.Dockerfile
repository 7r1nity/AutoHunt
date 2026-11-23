# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99 AUTOHUNT_DATA_ROOT=/var/lib/autohunt \
    AUTOHUNT_CONFIG_FILE=/run/autohunt-config/runtime.env
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium xvfb x11-utils openbox x11vnc novnc websockify socat fonts-noto-cjk ca-certificates libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 autohunt && useradd --uid 1000 --gid 1000 --create-home autohunt
WORKDIR /opt/autohunt
COPY LICENSE ./LICENSE
COPY autohunt/agent/pyproject.toml ./autohunt/agent/pyproject.toml
COPY autohunt/agent/app ./autohunt/agent/app
COPY autohunt/agent/evaluation/reports ./autohunt/agent/evaluation/reports
COPY autohunt/docs ./autohunt/docs
RUN pip install --no-cache-dir -e ./autohunt/agent \
    && python -m playwright --version \
    && mkdir -p /var/lib/autohunt /run/autohunt-config /run/autohunt-frontend /var/lib/mock-site \
    && chown -R autohunt:autohunt /var/lib/autohunt /run/autohunt-config /run/autohunt-frontend /var/lib/mock-site
COPY docker/*.py ./docker/
USER autohunt
EXPOSE 8000 6080
CMD ["python", "/opt/autohunt/docker/agent_entrypoint.py"]
