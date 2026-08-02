FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# The V1 upstream fixtures use source checkouts. Keep their runtime dependencies
# in the image so Agent and hidden-verifier commands stay offline.
RUN uv pip install --system --no-cache \
    pytest==9.0.3 \
    ruff==0.4.4 \
    annotated-types==0.7.0 \
    pydantic-core==2.47.0 \
    typing-extensions==4.15.0 \
    typing-inspection==0.4.2

WORKDIR /workspace

ENTRYPOINT []
