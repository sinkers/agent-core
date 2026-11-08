# The PoC image: agent-core plus the OpenShell SDK (the front door's launcher) and the PoC's Probe. The same image
# runs the front door (uvicorn --factory agentcore.frontdoor.app:app) and, in a sandbox, the worker
# (python -m agentcore.worker).
# Build from deploy/scale-to-zero-poc:  docker build --build-arg AGENT_CORE_IMAGE=agent-core:<tag> -f image/Dockerfile .
ARG AGENT_CORE_IMAGE=agent-core:dev
FROM ${AGENT_CORE_IMAGE}
USER root
RUN pip install --no-cache-dir openshell==0.1.6
COPY agents /app/agents
COPY policies /app/policies
USER 10001
