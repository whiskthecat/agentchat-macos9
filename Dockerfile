# The AgentChat relay: the bridge plus the OpenCode and Codex command-line tools.
#   docker compose up -d        (see README.md)
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git unar \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir Pillow==11.3.0
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g opencode-ai@1.19.4 @openai/codex@0.164.0 \
    && npm cache clean --force
COPY bridge /app/bridge
COPY agent_runtime /app/agent_runtime
COPY relay /app/relay
RUN useradd --create-home --home-dir /data --uid 1000 agentchat
USER agentchat
ENV HOME=/data \
    AGENT_WORKSPACE=/data/workspace \
    AGENT_STATE_FILE=/data/agentchat.sqlite3 \
    OPENCODE_URL=http://127.0.0.1:7777
VOLUME /data
EXPOSE 8765
CMD ["python3", "-u", "/app/relay/start.py"]
