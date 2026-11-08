{{- define "agentcore.postgres" -}}
{{- if .Values.postgres.enabled }}
# One Postgres instance, one database and role per agent.
apiVersion: v1
kind: ConfigMap
metadata:
  name: agents-postgres-init
data:
  init.sh: |
    #!/bin/sh
    set -eu
    for agent in {{ include "agentcore.agents" . }}; do
      pw_var="$(echo "$agent" | tr a-z A-Z)_DB_PASSWORD"
      pw="$(printenv "$pw_var")"
      psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<SQL
    DO \$\$ BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$agent') THEN
        CREATE ROLE $agent LOGIN PASSWORD '$pw';
      ELSE
        ALTER ROLE $agent PASSWORD '$pw';
      END IF;
    END \$\$;
    SELECT 'CREATE DATABASE $agent OWNER $agent' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$agent')\gexec
    REVOKE ALL ON DATABASE $agent FROM PUBLIC;
    SQL
    done
---
apiVersion: v1
kind: Service
metadata:
  name: agents-postgres
  labels: {app.kubernetes.io/name: agents-postgres}
spec:
  selector: {app.kubernetes.io/name: agents-postgres}
  ports: [{name: pg, port: 5432, targetPort: 5432}]
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: agents-postgres
spec:
  serviceName: agents-postgres
  replicas: 1
  selector:
    matchLabels: {app.kubernetes.io/name: agents-postgres}
  template:
    metadata:
      labels: {app.kubernetes.io/name: agents-postgres}
    spec:
      securityContext: {fsGroup: 70}
      containers:
        - name: postgres
          image: {{ .Values.postgres.image }}
          ports: [{containerPort: 5432, name: pg}]
          env:
            - {name: POSTGRES_USER, value: postgres}
            - name: POSTGRES_PASSWORD
              valueFrom: {secretKeyRef: {name: {{ .Values.postgres.adminSecret }}, key: password}}
            {{- range $a := .Values.global.agents }}
            - name: {{ upper $a }}_DB_PASSWORD
              valueFrom: {secretKeyRef: {name: agent-{{ $a }}, key: DB_PASSWORD}}
            {{- end }}
            - {name: PGDATA, value: /var/lib/postgresql/data/pgdata}
          lifecycle:
            # Re-applies roles on every start so rotated passwords take effect.
            postStart:
              exec:
                command: ["/bin/sh", "-c", "for i in $(seq 1 60); do pg_isready -U postgres -h 127.0.0.1 && break; sleep 2; done; sh /init/init.sh > /tmp/init.log 2>&1 || true"]
          readinessProbe:
            exec: {command: [pg_isready, -U, postgres, -h, 127.0.0.1]}
            periodSeconds: 10
          livenessProbe:
            exec: {command: [pg_isready, -U, postgres, -h, 127.0.0.1]}
            initialDelaySeconds: 30
            periodSeconds: 20
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {memory: 1Gi}
          volumeMounts:
            - {name: data, mountPath: /var/lib/postgresql/data}
            - {name: init, mountPath: /init}
      volumes:
        - name: init
          configMap: {name: agents-postgres-init}
  volumeClaimTemplates:
    - metadata: {name: data}
      spec:
        accessModes: [ReadWriteOnce]
        {{- with .Values.postgres.storageClass }}
        storageClassName: {{ . }}
        {{- end }}
        resources: {requests: {storage: {{ .Values.postgres.storage }}}}
{{- end }}
{{- end -}}
