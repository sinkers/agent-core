# Test agent (Probe)

One agent built from agent-core alone, with no organisation's files: no `org.yaml` (the engine runs neutral, in
UTC), no brand pack, no Teams, email, Jira, phone or memory. You talk to it over `POST /internal/chat`. Use it to
check a new engine build end to end, and as the test bed for scale-to-zero work (`docs/plans/scale-to-zero.md`).

It runs web search (SearXNG), files (Word/Excel/PowerPoint), the scheduler and call history, and loads one core
skill (`office-documents`). Budget: US$5 a day.

## Deploy

```bash
TAG=sha-<engine commit>   # an image CI pushed to ghcr.io/sinkers/agent-core
docker pull --platform linux/amd64 ghcr.io/sinkers/agent-core:$TAG
docker buildx build --platform linux/amd64 --build-arg AGENT_CORE_IMAGE=ghcr.io/sinkers/agent-core:$TAG \
    -t agent-core-test:$TAG --load deploy/test-agent
# get the image onto the cluster (push to a registry, or k3s: docker save ... | ssh <node> sudo k3s ctr -n k8s.io images import -)

NS=agents-test
kubectl create namespace $NS
# Secret agent-probe: DB_PASSWORD, INTERNAL_API_TOKEN (random), AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (Bedrock invoke only)
# Secret agents-postgres-admin: password (random)
helm dependency update deploy/test-agent
helm upgrade --install probe deploy/test-agent -n $NS --set global.image.tag=$TAG --wait
```

## Smoke test

```bash
kubectl -n $NS port-forward svc/agent-probe 18080:80 &
T=$(kubectl -n $NS get secret agent-probe -o jsonpath='{.data.INTERNAL_API_TOKEN}' | base64 -d)
curl -s localhost:18080/internal/chat -H "Authorization: Bearer $T" -H 'content-type: application/json' \
  -d '{"conversation_id":"test-1","user_id":"test-user","user_name":"Test","text":"Who are you?"}'
```

Checks worth running: a plain question (first responder), a web search, `schedule a task for 30 minutes from now`
then list them, and a short Word document with its outline. Scheduled results are delivered by Teams, which this
agent doesn't have, so the run shows in the logs and the task list but nothing is sent.

## Remove

```bash
helm uninstall probe -n $NS && kubectl delete namespace $NS
```
