{{- define "agentcore.searxng" -}}
{{- if .Values.searxng.enabled }}
# Web search for the agents (web.py provider "searxng"): our own SearXNG metasearch, no key and no cost.
# It queries Google, Bing, DuckDuckGo and others itself, so heavy use can draw CAPTCHAs; the agents cap themselves
# at 100 searches a day each. Internal only: no ingress, and only agent pods can reach it (NetworkPolicy).
apiVersion: v1
kind: ConfigMap
metadata:
  name: agents-searxng
  labels: {app.kubernetes.io/name: agents-searxng, app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}}
data:
  settings.yml: |
    use_default_settings: true
    engines:   # merged by name into the defaults
      - {name: brave, disabled: true}   # Brave suspends scrapers at once
    general:
      instance_name: {{ .Values.searxng.instanceName | default "agents search" | quote }}
      enable_metrics: false
    server:
      limiter: false          # only the agents call it, and they rate-limit themselves
      public_instance: false
      image_proxy: false
    search:
      safe_search: 1
      default_lang: "en-AU"
      formats: [html, json]   # json is what web.py reads
    outgoing:
      request_timeout: 5.0
      max_request_timeout: 10.0
---
apiVersion: v1
kind: Secret
metadata:
  name: agents-searxng
  labels: {app.kubernetes.io/name: agents-searxng, app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}}
type: Opaque
data:
  {{- $existing := lookup "v1" "Secret" .Release.Namespace "agents-searxng" }}
  {{- if and $existing $existing.data }}
  SEARXNG_SECRET: {{ index $existing.data "SEARXNG_SECRET" }}
  {{- else }}
  SEARXNG_SECRET: {{ randAlphaNum 48 | b64enc }}
  {{- end }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agents-searxng
  labels: {app.kubernetes.io/name: agents-searxng, app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}}
spec:
  replicas: 1
  selector:
    matchLabels: {app.kubernetes.io/name: agents-searxng}
  template:
    metadata:
      labels: {app.kubernetes.io/name: agents-searxng}
      annotations:
        checksum/settings: {{ .Values.searxng | toJson | sha256sum | trunc 16 }}
    spec:
      automountServiceAccountToken: false
      containers:
        - name: searxng
          image: {{ .Values.searxng.image }}
          ports: [{name: http, containerPort: 8080}]
          env:
            - {name: SEARXNG_BASE_URL, value: "http://agents-searxng:8080/"}
            - name: SEARXNG_SECRET
              valueFrom: {secretKeyRef: {name: agents-searxng, key: SEARXNG_SECRET}}
          readinessProbe: {httpGet: {path: /healthz, port: http}, periodSeconds: 10}
          livenessProbe: {httpGet: {path: /healthz, port: http}, initialDelaySeconds: 30, periodSeconds: 30}
          resources:
            requests: {cpu: 50m, memory: 128Mi}
            limits: {memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: settings, mountPath: /etc/searxng/settings.yml, subPath: settings.yml, readOnly: true}
      volumes:
        - name: settings
          configMap: {name: agents-searxng}
---
apiVersion: v1
kind: Service
metadata:
  name: agents-searxng
  labels: {app.kubernetes.io/name: agents-searxng, app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}}
spec:
  selector: {app.kubernetes.io/name: agents-searxng}
  ports: [{name: http, port: 8080, targetPort: http}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agents-searxng-agents-only
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: agents-searxng}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchExpressions: [{key: {{ include "agentcore.labelKey" $ }}, operator: Exists}]
      ports: [{port: 8080}]
{{- end }}
{{- end -}}
