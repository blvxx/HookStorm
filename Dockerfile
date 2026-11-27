# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/hookstorm \
    XDG_DATA_HOME=/home/hookstorm/.local/share

RUN groupadd --gid 10001 hookstorm \
    && useradd --no-log-init --uid 10001 --gid 10001 \
        --home-dir /home/hookstorm --create-home \
        --shell /usr/sbin/nologin hookstorm \
    && install -d -o 10001 -g 10001 /workspace

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-compile --no-index \
        --find-links=/wheels hookstorm \
    && rm -rf /wheels

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT ["hookstorm"]
CMD ["--help"]
