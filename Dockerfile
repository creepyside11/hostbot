FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip git ca-certificates tini gosu build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/emerald
COPY requirements.txt .
RUN python3 -m venv /opt/emerald/venv && /opt/emerald/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY main.py runner.py docker_backend.py github_worker.py private_repo_patch.py github_watch.py sqlite_inspector.py launcher.py entrypoint.sh ./
COPY sql/ ./sql/
RUN useradd --create-home --uid 10001 emerald && mkdir -p /app/data/emerald && chown -R emerald:emerald /app/data/emerald /opt/emerald
ENV DATA_DIR=/app/data/emerald PYTHONUNBUFFERED=1
ENTRYPOINT ["/bin/sh", "/opt/emerald/entrypoint.sh"]
