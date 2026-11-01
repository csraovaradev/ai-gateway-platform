{{- define "aigw.selector" -}}
app.kubernetes.io/name: aigw
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "aigw.labels" -}}
{{ include "aigw.selector" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
