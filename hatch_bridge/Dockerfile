# Plain Python base image (multi-arch). Deliberately NOT a Home Assistant
# base image: those ship s6-overlay, which conflicts with the init process
# the Supervisor starts app containers with ("s6-overlay-suexec: fatal:
# can only run as pid 1", instant exit 100). This app needs nothing from
# the HA base image: SUPERVISOR_TOKEN is injected as an env var and
# /data/options.json is mounted by the Supervisor for every app.
FROM python:3.13-alpine

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

# Bridge dependencies
COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache py3-pip \
    && pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Bridge application
COPY app.py /app/app.py

ARG BUILD_VERSION=1.1.0
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

EXPOSE 8099

CMD ["python3", "-u", "/app/app.py"]
