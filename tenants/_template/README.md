# Tenant template

Copy this folder to `tenants/<team>/` and replace `TEAM_NAME` to onboard a team.

Platform owns (here, in platform-gitops):
- `infra/` — namespace, ResourceQuota, LimitRange, NetworkPolicy (cloud-agnostic isolation).
- `controlplane/` — the team AppProject + the workload ApplicationSet.

Team owns (in their own `<team>-gitops` repo):
- `common/` — team-owned **namespaced** manifests applied to **every** cluster the team runs on
  (cloud/env/region-agnostic: ServiceAccounts, Istio `AuthorizationPolicy`/`PeerAuthentication`,
  extra `NetworkPolicy`, `ConfigMap`, PDB, ...).
- `clusters/<cloud>/<env>/<region>/` — team-owned **per-cluster** namespaced manifests, keyed by the
  cluster's cloud/env/region labels (mirrors `platform-terraform/clusters/<cloud>/<env>/<region>`).
  The ESO **SecretStore** lives here: it points at the team's own **env+region-local** vault (a prod
  vault is a different vault/subscription than sbx) with workload-identity / IRSA auth, and keeps a
  **constant NAME** across clusters so the golden chart's `externalSecret.storeRef` is agnostic.
- Both folders are combined into one `tenant-<team>-config` Application (under the **team** AppProject)
  by the platform `tenants-config` ApplicationSet using Argo CD **multiple sources** — the legitimate
  use (compose one owner's manifests), not a way to merge platform-owned infra with team content.
  Directory recursion means teams add manifest files with no platform wiring change.
  Env+region-local vaults give isolation, latency, and data residency; the team syncs secret material
  across vaults (AWS: native replica secrets; Azure: backup/restore or a pipeline). Teams CANNOT
  create cluster-scoped resources such as `ClusterSecretStore` (platform-only; team AppProject keeps
  `clusterResourceWhitelist: []`).
- Per-service `externalSecret.*` values in the golden chart (ExternalSecret is per-service).
