FROM node:22-bookworm-slim AS node
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && python -m pip install --no-cache-dir playwright==1.64.1 psutil==7.2.2 \
    && mkdir -p /work /output /source /runner \
    && chown -R pwuser:pwuser /work /output
COPY runner.py /runner/runner.py
USER pwuser
WORKDIR /work
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CI=true
ENTRYPOINT ["python", "-u", "/runner/runner.py"]
