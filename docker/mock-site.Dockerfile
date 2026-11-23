# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/mock-site
COPY LICENSE ./LICENSE
COPY mock-site/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 autohunt && useradd --uid 1000 --gid 1000 --create-home autohunt \
    && mkdir /data && chown autohunt:autohunt /data
COPY mock-site/app ./app
COPY mock-site/common ./common
COPY mock-site/frontend ./frontend
USER autohunt
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "15"]
