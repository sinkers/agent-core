{{- define "agent.name" -}}agent-{{ required "agentName is required" .Values.agentName }}{{- end -}}
{{- define "agent.labels" -}}
app.kubernetes.io/name: {{ include "agent.name" . }}
app.kubernetes.io/part-of: {{ .Values.global.partOf | default "agent-core" }}
{{ .Values.global.labelDomain | default "agent-core" }}/agent: {{ .Values.agentName }}
{{- end -}}
{{- define "agent.selector" -}}
app.kubernetes.io/name: {{ include "agent.name" . }}
{{- end -}}
