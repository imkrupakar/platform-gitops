# Tenant template

Copy this folder to `tenants/<team>/` and replace `TEAM_NAME` to onboard a team.

Platform owns (here, in platform-gitops):
- `infra/` — namespace, ResourceQuota, LimitRange, NetworkPolicy (cloud-agnostic isolation).
- `controlplane/` — the team AppProject + the workload ApplicationSet.

Team owns (in their own `<team>-gitops` repo):
- `infra/<cloud>/secretstore.yaml` — the ESO **SecretStore** (per namespace, cloud-specific),
  pointing at the team's own vault with workload-identity / IRSA auth. The platform
  `tenants-secretstore` ApplicationSet applies it automatically, selecting `<cloud>` from the
  cluster's label. Teams CANNOT create cluster-scoped `ClusterSecretStore` (platform-only).
- Per-service `externalSecret.*` values in the golden chart (ExternalSecret is per-service).
