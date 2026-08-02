FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# The workspace source is mounted at runtime, so install its locked runtime and
# test dependencies into the image rather than relying on a host virtualenv.
COPY pyproject.toml uv.lock /tmp/pico/
RUN uv export --directory /tmp/pico --locked --all-groups --no-emit-project \
        --format requirements.txt --output-file /tmp/pico/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/pico/requirements.txt \
    && rm /tmp/pico/requirements.txt

# tiktoken lazily downloads o200k_base. Preload it into an image-owned cache so
# the runtime sandbox can stay offline and use a read-only root filesystem.
ENV TIKTOKEN_CACHE_DIR=/opt/pico/tiktoken-cache
RUN mkdir --parents "$TIKTOKEN_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

WORKDIR /workspace

ENTRYPOINT []
