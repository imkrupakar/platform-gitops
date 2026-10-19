# Tenant template

Copy this folder to `tenants/<team>/` and replace `TEAM_NAME` to onboard a team.

Platform owns (here, in platform-gitops):
- `infra/` — namespace, ResourceQuota, LimitRange, NetworkPolicy (cloud-agnostic isolation).
- `controlplane/` — the team AppProject + the workload ApplicationSet.

Team owns (in their own `<team>-gitops` repo):
- `infra/<cloud>/<env>/<region>/secretstore.yaml` — the ESO **SecretStore** (per namespace, keyed by
  cloud + environment + region, mirroring `platform-terraform/clusters/<cloud>/<env>/<region>`),
  pointing at the team's own **env+region-local** vault (a prod vault is a different vault/subscription
  than sbx) with workload-identity / IRSA auth. The platform `tenants-secretstore` ApplicationSet
  applies it automatically, selecting `<cloud>`/`<env>`/`<region>` from the cluster's labels, so each
  cluster pulls its own store while the store NAME stays constant (chart `storeRef` is
  cloud/env/region-agnostic). Env+region-local vaults give isolation, latency, and data residency; the
  team syncs secret material across vaults (AWS: native replica secrets; Azure: backup/restore or a
  pipeline). Teams CANNOT create cluster-scoped `ClusterSecretStore` (platform-only).
- Per-service `externalSecret.*` values in the golden chart (ExternalSecret is per-service).
