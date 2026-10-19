# Tenant template

Copy this folder to `tenants/<team>/` and replace `TEAM_NAME` to onboard a team.

Platform owns (here, in platform-gitops):
- `namespace/` — Namespace, ResourceQuota, LimitRange, NetworkPolicy (cloud-agnostic isolation).
- `controlplane/` — the team AppProject + two ApplicationSets: `appset.yaml` (workloads, per service)
  and `config-appset.yaml` (team namespaced config, per cluster). Both are installed into argocd by
  the `tenant-<team>-cp` Application, so there is no separate top-level appset for team config.

Team owns (in their own `<team>-gitops` repo):
- `config/envs/<env>/<cloud>/<region>/` — team-owned **per-cluster namespaced** manifests, keyed by
  the cluster's env/cloud/region labels (same `envs/<env>/<cloud>/<region>` order the team's `apps/`
  use). The ESO **SecretStore** lives here (later: ServiceAccounts, Istio
  `AuthorizationPolicy`/`PeerAuthentication`, extra `NetworkPolicy`, `ConfigMap`, PDB). It points at
  the team's own **env+region-local** vault (a prod vault is a different vault/subscription than sbx)
  with workload-identity / IRSA auth, and keeps a **constant NAME** across clusters so the golden
  chart's `externalSecret.storeRef` is agnostic.
- The team's `config-appset.yaml` (in `controlplane/`) creates one `<team>-config-<env>-<region>`
  Application per cluster (under the **team** AppProject) that syncs the matching
  `config/envs/<env>/<cloud>/<region>/` folder (directory recurse → add manifest files with no wiring
  change). A cluster with no such folder renders nothing (gated by a git-directories generator).
  Env+region-local vaults give isolation, latency, and data residency; the team syncs secret material
  across vaults (AWS: native replica secrets; Azure: backup/restore or a pipeline). Teams CANNOT
  create cluster-scoped resources such as `ClusterSecretStore` (platform-only; team AppProject keeps
  `clusterResourceWhitelist: []`).
- An `eso-reader` **ServiceAccount** next to the SecretStore (annotated with the team's Azure UAMI
  client-id or AWS IRSA role ARN). The store must reference it; never `default` (app pods would
  inherit vault access) and never omitted (ESO would fall back to its controller credentials).
  `config/secretstore-guard` rejects any tenant SecretStore that breaks this or uses static creds.
- **Hard rule for the team's cloud identity:** its Azure federated credential, or its AWS role trust
  policy (StringEquals on `sub` AND `aud`), trusts exactly
  `system:serviceaccount:<team>:eso-reader` on each cluster's OIDC issuer, with no wildcards. This exact
  subject is what stops another team from annotating its own SA with your identity.
- Per-service `externalSecret.*` values in the golden chart (ExternalSecret is per-service).
