# Tenant template

Copy this folder to `tenants/<team>/` and replace `TEAM_NAME` to onboard a team.

Platform owns (here, in platform-gitops):
- `infra/` — namespace, ResourceQuota, LimitRange, NetworkPolicy (cloud-agnostic isolation).
- `controlplane/` — the team AppProject + the workload ApplicationSet.

Team owns (in their own `<team>-gitops` repo):
- `infra/<cloud>/<region>/secretstore.yaml` — the ESO **SecretStore** (per namespace, cloud- AND
  region-specific), pointing at the team's own **region-local** vault with workload-identity / IRSA
  auth. The platform `tenants-secretstore` ApplicationSet applies it automatically, selecting
  `<cloud>`/`<region>` from the cluster's labels, so each regional cluster pulls its own region-local
  store while the store NAME stays constant (chart `storeRef` is cloud/region-agnostic). Region-local
  vaults give latency, blast-radius isolation, and data residency; the team syncs secret material
  across regional vaults (AWS: native multi-region replica secrets; Azure: backup/restore or a
  pipeline). Teams CANNOT create cluster-scoped `ClusterSecretStore` (platform-only).
- Per-service `externalSecret.*` values in the golden chart (ExternalSecret is per-service).
