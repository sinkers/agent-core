{{- define "agentcore.mcpservers" -}}
{{- /*
MCP servers (PLT-020): one Deployment per server identity, reachable only by the agent that
owns it (NetworkPolicy). Credentials come from Secret mcp-<name> (scripts/agents-deploy.sh).
*/ -}}
{{- range $s := .Values.mcpServers }}
{{- if $s.enabled }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-{{ $s.name }}
  labels: {app.kubernetes.io/name: mcp-{{ $s.name }}, app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}}
spec:
  replicas: 1
  selector:
    matchLabels: {app.kubernetes.io/name: mcp-{{ $s.name }}}
  template:
    metadata:
      labels: {app.kubernetes.io/name: mcp-{{ $s.name }}, app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}}
    spec:
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: server
          image: "{{ $.Values.global.image.repository }}:{{ $.Values.global.image.tag }}"
          imagePullPolicy: {{ $.Values.global.image.pullPolicy }}
          command: ["python", "-m", "{{ $s.module }}"]
          ports: [{name: http, containerPort: 8080}]
          envFrom:
            - secretRef: {name: mcp-{{ $s.name }}}
          env:
            {{- range $k, $v := $s.env }}
            - {name: {{ $k }}, value: {{ $v | quote }}}
            {{- end }}
          readinessProbe: {httpGet: {path: /healthz, port: http}, periodSeconds: 10}
          livenessProbe: {httpGet: {path: /healthz, port: http}, initialDelaySeconds: 10, periodSeconds: 20}
          resources: {requests: {cpu: 50m, memory: 96Mi}, limits: {memory: 256Mi}}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
---
apiVersion: v1
kind: Service
metadata:
  name: mcp-{{ $s.name }}
spec:
  selector: {app.kubernetes.io/name: mcp-{{ $s.name }}}
  ports: [{name: http, port: 80, targetPort: http}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: mcp-{{ $s.name }}-owner-only
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: mcp-{{ $s.name }}}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {{ "{" }}{{ include "agentcore.labelKey" $ }}: {{ $s.owner }}{{ "}" }}
      ports: [{port: 8080}]
{{- end }}
{{- end }}
{{- end -}}
