{{- /* Labels every agent-core resource carries; a deployment sets global.partOf and global.labelDomain. */ -}}
{{- define "agentcore.partOf" -}}{{ .Values.global.partOf | default "agent-core" }}{{- end -}}
{{- define "agentcore.labelKey" -}}{{ .Values.global.labelDomain | default "agent-core" }}/agent{{- end -}}
{{- /* The agents' keys (global.agents), e.g. [ada, eve]: one database role and Secret agent-<key> each. */ -}}
{{- define "agentcore.agents" -}}{{ required "global.agents is required (the agents' keys)" .Values.global.agents | join " " }}{{- end -}}
