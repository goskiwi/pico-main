FROM pico/real-oss-suite:latest

COPY validation/official_public_test_requirements.txt /tmp/official-public-tests.txt
RUN uv pip install --system --no-cache -r /tmp/official-public-tests.txt

WORKDIR /workspace
ENTRYPOINT []
