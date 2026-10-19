# platform-gitops

Source of truth for Argo CD configuration, platform add-ons, AppProjects, ApplicationSets,
and tenant isolation (namespaces / quota / NetworkPolicy). Owned by **Platform
Engineering**. Application teams never commit here except to onboard (a single PR adding a
`tenants/<team>/` folder).

This repo is **cluster-agnostic**: the identical config is pulled by the **standalone Argo
CD on every cluster**. Each cluster's environment is discovered from the labeled in-cluster
secret created by the cluster-provisioning Terraform (separate repo
`cluster-platform-terraform`). There is no hub and no cross-cluster registry here.

## Directory map

```
platform-gitops/
├── bootstrap/
│   └── app-of-apps/                # what the per-cluster root Application points at
│       ├── 00-argocd-self.yaml     # Argo CD manages its own config (→ argocd/base)
│       ├── 08-projects.yaml        # platform AppProjects (→ projects/)
│       └── 10-appsets.yaml         # platform ApplicationSets (→ appsets/)
│
├── argocd/                         # Argo CD self-config (HOT-RELOADED cm/rbac only)
│   └── base/{kustomization,argocd-cm,argocd-rbac-cm,repo-golden-charts}.yaml
│                                   # repo-golden-charts: declares the OCI chart registry (enableOCI) to Argo CD
│                                   # NB: argocd-cmd-params-cm is owned by the Helm install
│                                   # (startup params need a controlled pod restart).
│
├── environments/                   # per-CLUSTER pinned ADD-ON versions (the golden chart is pinned by teams, in their leaves)
│   └── <env>/<cloud>/<region>/versions.yaml
│                                   # promote per cloud/region: sbx → dev → stg → prd, one
│                                   # cluster at a time (editing one never touches another)
│
├── projects/                       # platform-owned AppProjects
│   ├── platform-addons.yaml
│   └── platform-tenants.yaml
│
├── addons/                         # platform add-on values (ESO, cert-manager, ingress, rollouts)
│   ├── external-secrets/values-common.yaml
│   ├── cert-manager/values-common.yaml
│   ├── ingress-nginx/values-common.yaml
│   └── argo-rollouts/values-common.yaml
│
├── appsets/                        # ApplicationSets (all target the LOCAL cluster)
│   ├── addons.yaml                 # (cluster(local) × env versions) × add-on list → add-ons
│   ├── platform-config.yaml        # git(config/*) → platform config that is not a Helm add-on
│   ├── tenants-controlplane.yaml   # git(tenants/*) → per-tenant AppProject + workload appset
│   └── tenants-infra.yaml          # (cluster × git tenants/*) → per-tenant ns/quota/netpol
│                                   #   (the ESO SecretStore is team-owned; see tenants/_template/README.md)
│
├── config/                         # platform config applied on every cluster
│   ├── cluster-issuers/            # cert-manager ClusterIssuers (letsencrypt-staging / -prod) used by ingress.clusterIssuer
│   └── secretstore-guard/          # ValidatingAdmissionPolicy: tenant SecretStores must use eso-reader + workload identity/IRSA
│
├── tools/                          # validators (run in CI, and locally)
│   ├── validate_platform.py        # this repo's consistency: fleet <-> versions, tenant layout, template drift, chart source
│   └── check_team_layout.py        # the team-repo checks (leaf layout, goldenChart policy + ring order, render with the PINNED chart)
│
├── .github/workflows/              # validate.yml (this repo's PR check), validate-team-repo.yml (REUSABLE; team repos call it)
├── CODEOWNERS                      # every merge here reaches every cluster
│
├── tenants/                        # ONE folder per tenant = isolation boundary
│   ├── _template/                  # copy to onboard a new tenant (full infra set)
│   ├── team-alpha/
│   │   ├── controlplane/{project.yaml, appset.yaml, config-appset.yaml} # config-appset syncs the team's own
│   │   │                                                                #   config/envs/<env>/<cloud>/<region>/ (SecretStore + eso-reader SA)
│   │   └── infra/{namespace,resourcequota,networkpolicy}.yaml          # cloud-agnostic
│   └── team-beta/ (same shape)
│
└── registry/                       # tenants.yaml — human-readable team -> repo index.
    └── tenants.yaml                #   (Cluster inventory lives in cluster-platform-terraform/fleet.yaml.)
```

## Argo CD config ownership (no split-brain)

Config is split by reload semantics so Helm and Argo CD never fight over the same object:

| Object | Owner | Why |
| --- | --- | --- |
| `argocd-cmd-params-cm` | **Helm install** (`configs.params`) | consumed at startup → needs a controlled pod restart |
| `argocd-cm`, `argocd-rbac-cm` | **GitOps** (`argocd/base`, via `configs.*.create: false`) | hot-reloaded → `argocd-self` is the single writer |

Tenant RBAC lives in each tenant's AppProject `roles`; the global `argocd-rbac-cm` only
holds platform-admin policy. Onboarding a team needs no edit to any global file.

## Versioned promotion (add-ons; golden chart is team-owned)

`environments/<env>/<cloud>/<region>/versions.yaml` pins each **add-on** version for **that
specific cluster**. The add-ons ApplicationSet selects the file via the cluster's
`environment` + `cloud` + `region` labels (matrix: cluster generator → templated git-files
path) and templates `targetRevision` from it. Promotion is a CODEOWNERS-gated PR per cluster:
bump sbx, soak, then advance the matching dev → stg → prd cluster file. Because each cluster
has its own file, a dev bump on Azure/eastus never touches dev on AWS — you promote per cloud
and per region, not a whole environment at once.

**Each team owns its golden chart version.** They set `goldenChart` directly in their own
cluster **leaf** (`…/<region>/values.yaml`, the same file that holds the image pin), so a team
adopts a new chart version on its own schedule, independently of other teams and other
services on the same cluster. The team workload ApplicationSet still uses the cluster's
`environment`/`cloud`/`region` labels to select the team's own config, layered
`base → envs/<env> → envs/<env>/<cloud> → envs/<env>/<cloud>/<region>`
(all optional except base). The ApplicationSet also passes `global.cloud/region/environment`
to the golden chart so cloud-specific bits (workload-identity labels) are rendered by the
chart, not hardcoded by teams. See `team-alpha-gitops/README.md`.

## What flows from one root Application (identical on every cluster)

```
root (Terraform applies, per cluster) ──▶ bootstrap/app-of-apps/
   ├── 00-argocd-self ──▶ argocd/base                     (Argo CD cm + rbac)
   ├── 08-projects    ──▶ projects/                       (platform AppProjects)
   └── 10-appsets     ──▶ appsets/
         ├── addons.yaml             ──▶ ESO, cert-manager, ingress, rollouts (local cluster)
         ├── tenants-controlplane    ──▶ per-tenant AppProject + workload appset
         └── tenants-infra           ──▶ per-tenant ns/quota/netpol/ESO store (local cluster)
                     │
                     └── tenant workload appset ──▶ golden chart + team values ──▶ workloads
                             (env selected from the local cluster's `environment` label)
```

Because every cluster is one environment, a `dev` cluster renders `dev` values and a `prd`
cluster renders `prd` values from the SAME Git — no per-cluster config duplication. Where
clusters of one environment differ (cloud, region), only the optional cloud/leaf layers differ.

## Onboarding a tenant = one PR

1. `cp -r tenants/_template tenants/team-gamma` and replace `TEAM_NAME`, set quota, and
   The team adds its SecretStore + `eso-reader` ServiceAccount in its own repo under
   `config/envs/<env>/<cloud>/<region>/` (see `tenants/_template/README.md`).
2. Merge. On every cluster, the fan-out ApplicationSets create gamma's AppProject,
   namespace, quota, NetworkPolicy, workload ApplicationSet, and config ApplicationSet with
   **zero kubectl**. Gamma's first service goes live when team-gamma commits a
   `values.yaml` to their own `team-gamma-gitops` repo.

## Recovery

Re-running the cluster's Terraform stack reinstalls Argo CD and re-applies the single root
Application; the app-of-apps rebuilds that cluster's entire platform and tenant workloads
from Git. The only non-Git input is the GitHub App key (from the secret store).
