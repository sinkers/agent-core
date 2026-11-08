{{- define "agentcore.hindsight" -}}
{{- if .Values.hindsight.enabled }}
# Long-term memory (PLT-031, ADR-0006): Hindsight with embedded Postgres, Bedrock Haiku (AU)
# for fact extraction, local embeddings and reranker. One bank per agent. Only agent pods
# can reach it (NetworkPolicy).
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: agents-hindsight-data
  annotations: {helm.sh/resource-policy: keep}
spec:
  accessModes: [ReadWriteOnce]
  {{- with .Values.postgres.storageClass }}
  storageClassName: {{ . }}
  {{- end }}
  resources: {requests: {storage: {{ .Values.hindsight.storage }}}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agents-hindsight
  labels: {app.kubernetes.io/name: agents-hindsight, app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector:
    matchLabels: {app.kubernetes.io/name: agents-hindsight}
  template:
    metadata:
      labels: {app.kubernetes.io/name: agents-hindsight}
    spec:
      automountServiceAccountToken: false
      containers:
        - name: hindsight
          image: {{ .Values.hindsight.image }}
          ports: [{name: http, containerPort: 8888}]
          env:
            - {name: HINDSIGHT_API_LLM_PROVIDER, value: bedrock}
            - {name: HINDSIGHT_API_LLM_MODEL, value: {{ .Values.hindsight.llmModel | quote }}}
            - {name: HINDSIGHT_API_HOST, value: "0.0.0.0"}
            - {name: HINDSIGHT_API_PORT, value: "8888"}
            - {name: HINDSIGHT_API_LOG_LEVEL, value: info}
            - {name: AWS_DEFAULT_REGION, value: {{ .Values.global.awsRegion | default "us-east-1" }}}
            - name: AWS_ACCESS_KEY_ID
              valueFrom: {secretKeyRef: {name: {{ .Values.hindsight.awsSecret | default (printf "agent-%s" (first .Values.global.agents)) }}, key: AWS_ACCESS_KEY_ID}}
            - name: AWS_SECRET_ACCESS_KEY
              valueFrom: {secretKeyRef: {name: {{ .Values.hindsight.awsSecret | default (printf "agent-%s" (first .Values.global.agents)) }}, key: AWS_SECRET_ACCESS_KEY}}
          readinessProbe: {httpGet: {path: /health, port: http}, periodSeconds: 10, failureThreshold: 30}
          livenessProbe: {httpGet: {path: /health/live, port: http}, initialDelaySeconds: 60, periodSeconds: 30}
          resources:
            requests: {cpu: 250m, memory: 1Gi}
            limits: {memory: 4Gi}
          volumeMounts:
            - {name: data, mountPath: /home/hindsight/.pg0}
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: agents-hindsight-data}
---
apiVersion: v1
kind: Service
metadata:
  name: agents-hindsight
spec:
  selector: {app.kubernetes.io/name: agents-hindsight}
  ports: [{name: http, port: 8888, targetPort: http}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agents-hindsight-agents-only
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: agents-hindsight}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchExpressions: [{key: {{ include "agentcore.labelKey" $ }}, operator: Exists}]
      ports: [{port: 8888}]
{{- end }}
{{- end -}}
