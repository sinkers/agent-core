# agent-core: the engine image, with no agents in it. A deployment builds FROM it and adds its agents (agent.yaml +
# prompts under /app/agents/<name>), skills, reference library, brand pack, method snapshot and org.yaml.
# Build from this folder:  docker build -t agent-core:dev .
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml LICENSE ./
COPY src src
COPY plugins plugins
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
# ffmpeg converts Teams call recordings to 16 kHz PCM for transcription (voice.py).
# LibreOffice (headless, no GUI) renders Word, Excel and PowerPoint pages so the agents can see what they made
# (office.py), with open fonts metric-compatible with Georgia, Calibri and Consolas (fonts/).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY fonts/*.ttf /usr/local/share/fonts/agent-core/
COPY fonts/30-metric-substitutes.conf /etc/fonts/conf.d/30-metric-substitutes.conf
RUN fc-cache -f >/dev/null
RUN useradd --uid 10001 --create-home agent
COPY --from=build /install /usr/local
# Where a deployment's image puts its files (AGENT_DIR, set per agent, is /app/agents/<name>). Without org.yaml the
# engine runs neutral (UTC, no internal domains); without a brand pack, documents use the built-in plain style.
ENV ORG_FILE=/app/org.yaml SKILLS_DIR=/app/skills KNOWLEDGE_DIR=/app/knowledge TEMPLATES_DIR=/app/templates \
    METHOD_DIR=/app/method PYTHONUNBUFFERED=1
# Drawing DXF files (dxf.py): matplotlib and fontconfig caches live in /tmp, the writable path at runtime
ENV MPLCONFIGDIR=/tmp/matplotlib XDG_CACHE_HOME=/tmp/.cache
USER 10001
EXPOSE 8080
CMD ["uvicorn", "agentcore.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
