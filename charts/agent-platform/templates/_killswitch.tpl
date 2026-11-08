{{- define "agentcore.killswitch" -}}
{{- /*
Kill switch. One key per agent: "run" or "stop". Values already in the cluster
win over chart values, so `helm upgrade` never silently re-enables a stopped agent.
Flip with: kubectl -n <ns> patch cm agents-killswitch -p '{"data":{"<agent>":"stop"}}'
*/ -}}
{{- $existing := (lookup "v1" "ConfigMap" .Release.Namespace .Values.global.killswitchConfigMap) -}}
{{- $current := dict -}}
{{- if $existing }}{{- $current = (default (dict) $existing.data) -}}{{- end }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Values.global.killswitchConfigMap }}
  labels:
    app.kubernetes.io/part-of: {{ include "agentcore.partOf" $ }}
  annotations:
    helm.sh/resource-policy: keep
data:
  {{- range $agent, $value := .Values.killswitch }}
  {{ $agent }}: {{ (get $current $agent) | default $value | quote }}
  {{- end }}
---
# The agent's owner and the on-call engineer can flip the switch; agents can only read it.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: agents-killswitch-operator
rules:
  - apiGroups: [""]
    resources: [configmaps]
    resourceNames: [{{ .Values.global.killswitchConfigMap }}]
    verbs: [get, patch, update]
{{- end -}}
