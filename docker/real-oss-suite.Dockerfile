FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock /tmp/pico/
RUN uv export --directory /tmp/pico --locked --all-groups --no-emit-project \
        --format requirements.txt --output-file /tmp/pico/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/pico/requirements.txt \
    && uv pip install --system --no-cache markupsafe==3.0.3

ENV TIKTOKEN_CACHE_DIR=/opt/pico/tiktoken-cache
RUN mkdir --parents "$TIKTOKEN_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

WORKDIR /workspace
ENTRYPOINT []
