# The test agent on agent-core: the engine image plus one agent folder. No org.yaml, so the engine runs neutral.
# Build:  docker build --build-arg AGENT_CORE_IMAGE=agent-core:<tag> -t agent-core-test:<tag> deploy/test-agent
ARG AGENT_CORE_IMAGE=agent-core:dev
FROM ${AGENT_CORE_IMAGE}
COPY agents /app/agents
