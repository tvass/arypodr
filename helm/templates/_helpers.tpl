{{/*
Resource name — the release name, truncated to 63 chars.
*/}}
{{- define "arypodr.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels attached to every resource.
*/}}
{{- define "arypodr.labels" -}}
app.kubernetes.io/name: arypodr
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels used by the Deployment and Service.
*/}}
{{- define "arypodr.selectorLabels" -}}
app.kubernetes.io/name: arypodr
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
