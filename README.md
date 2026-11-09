# platform-gitops

> 📖 **New here? Start with the [Platform Guide](docs/platform-guide.md)** — the end-to-end tour of how
> the multi-cluster, multi-cloud platform fits together and how to add a service, region, cloud, team or
> environment. This README covers just the design of *this* repo.

The single source of truth for Argo CD configuration, platform add-ons, AppProjects,
ApplicationSets, and tenant isolation (namespace / quota / NetworkPolicy). Owned by
**Platform Engineering**.

Application teams never commit here — except once, to onboard: a single PR that adds a
`tenants/<team>/` folder.

## How it works in one paragraph

This repo is **cluster-agnostic**: the identical config is pulled by the **standalone Argo CD
running on every cluster**. A cluster learns *who it is* (environment, cloud, region) from a
labeled in-cluster secret created by the cluster-provisioning Terraform (separate repo
`platform-terraform`). ApplicationSets read those labels to render the
right versions and values for that cluster. There is no hub and no cross-cluster registry here —
every cluster manages only itself, from the same Git.

## Directory map

```
platform-gitops/
├── bootstrap/app-of-apps/      # what each cluster's root Application points at (one per concern)
│   ├── 05-argocd-repos.yaml    #   → argocd/repositories   (register the golden-charts OCI registry)
│   ├── 08-projects.yaml        #   → argocd/projects       (platform AppProjects)
│   └── 10-appsets.yaml         #   → argocd/appsets        (platform ApplicationSets)
│
├── argocd/                     # Argo CD control-plane config (repositories + projects + appsets)
│   ├── repositories/           #   repository registrations applied by Argo CD (hot-loaded)
│   │   └── repo-golden-charts.yaml #   the ghcr.io OCI chart registry (enableOCI)
│   ├── projects/               #   platform-owned AppProjects
│   │   ├── platform-addons.yaml
│   │   └── platform-tenants.yaml
│   └── appsets/                #   ApplicationSets — all target the LOCAL cluster
│       ├── platform-addons.yaml    #   add-on list × env versions        → ESO, Istio, Argo Rollouts
│       ├── platform-config.yaml    #   env versions                      → platform-config chart
│       ├── tenants-controlplane.yaml #  git(tenants/*)                   → per-tenant AppProject + workload appset
│       └── tenants-namespace.yaml  #   cluster × git(tenants/*)          → per-tenant ns / quota / netpol
│
├── envs/                       # per-CLUSTER version pins (add-ons + platformConfig)
│   └── <env>/<cloud>/<region>/versions.yaml
│
├── addons/                     # add-on Helm values, layered: base + envs/<env>/<cloud>/<region>
│   └── <addon>/base/values.yaml
│
├── config/                     # values for the platform-config chart, layered: base + envs/<env>
│   ├── base/values.yaml
│   └── envs/<env>/values.yaml
│
├── tenants/                    # ONE folder per tenant = one isolation boundary
│   ├── _template/              #   copy this to onboard (controlplane/ + namespace/)
│   └── team-alpha/
│       ├── controlplane/       #   project.yaml, appset.yaml, config-appset.yaml
│       └── namespace/          #   namespace, resourcequota, networkpolicy (platform-owned)
│
├── registry/tenants.yaml       # human-readable team → repo index (cluster list lives in platform-terraform/fleet.yaml)
├── tools/                      # onboard-tenant.sh + CI validators (validate_platform.py, check_team_layout.py)
├── .github/workflows/          # validate.yml (this repo) + validate-team-repo.yml (reusable, called by team repos)
└── CODEOWNERS                  # every merge here reaches every cluster
```

## What flows from the one root Application

Terraform applies a single root Application per cluster; everything below is pure GitOps and is
identical on every cluster:

```
root  ──▶  bootstrap/app-of-apps/
   ├── 05-argocd-repos  ──▶  argocd/repositories/      (golden-charts OCI registry)
   ├── 08-projects      ──▶  argocd/projects/          (platform AppProjects)
   └── 10-appsets       ──▶  argocd/appsets/
         ├── addons             ──▶  ESO, Istio (base + istiod), Argo Rollouts
         ├── platform-config    ──▶  platform-config chart (Istio Gateway + SecretStore guard)
         ├── tenants-controlplane ─▶  per-tenant AppProject + workload appset
         └── tenants-namespace  ──▶  per-tenant namespace / quota / NetworkPolicy
                 │
                 └── tenant workload appset ──▶  golden chart + team values ──▶ workloads
```

Because every cluster *is* one environment, a `dev` cluster renders `dev` values and a `prd`
cluster renders `prd` values from the same Git — no per-cluster config duplication. Where clusters
of the same environment differ (cloud, region), only the optional cloud/region overlay files differ.

## Argo CD config ownership

Argo CD's own settings are split by who can safely write them, so nothing fights over an object:

| Object | Owner | Why |
| --- | --- | --- |
| `argocd-cm`, `argocd-rbac-cm`, `argocd-cmd-params-cm` | **Helm install** (in `platform-terraform`) | installed and upgraded with Argo CD itself; params need a controlled pod restart |
| `repo-golden-charts` repository secret | **This repo** (`argocd/repositories/`) | a plain registration Argo CD hot-loads — safe to manage from Git |

Tenant RBAC is **not** global: it lives in each tenant's AppProject `roles` (see
`tenants/team-alpha/controlplane/project.yaml`). The cluster-wide `argocd-rbac-cm` only holds
platform-admin policy, so onboarding a team edits no global file.

## Versioned promotion

`envs/<env>/<cloud>/<region>/versions.yaml` pins, **for that one cluster**, each add-on version and
the `platformConfig` (platform-config chart) version. ApplicationSets select the file from the
cluster's `environment` + `cloud` + `region` labels and template `targetRevision` from it.

Promotion is a CODEOWNERS-gated PR, **one cluster at a time**: bump `sbx`, soak, then advance
`dev → stg → prd`. Because every cluster owns its own file, a dev bump on Azure/centralus never
touches dev on AWS — you promote per cloud and per region, never a whole environment at once.

**Teams own their own chart version.** Each team sets `goldenChart` in its own cluster leaf file
(the same `…/<region>/values.yaml` that holds its image pin), in its own `<team>-gitops` repo. So a
team adopts a new chart version on its own schedule, independent of other teams and other services
on the same cluster. Team values layer `base → envs/<env> → envs/<env>/<cloud> →
envs/<env>/<cloud>/<region>` (all optional except `base`), and the workload ApplicationSet passes
`global.cloud/region/environment` to the golden chart so cloud-specific bits (e.g. workload-identity
labels) are rendered by the chart rather than hardcoded by teams. See `team-alpha-gitops/README.md`.

## Onboarding a tenant = one PR

1. Run `tools/onboard-tenant.sh <team>`. It copies `tenants/_template/` to `tenants/<team>/`,
   substitutes `TEAM_NAME`, and registers the team in `registry/tenants.yaml`. Review the quota in
   `tenants/<team>/namespace/resourcequota.yaml`. The team authors its own SecretStore and
   `eso-reader` ServiceAccount in **its** repo (see `tenants/_template/README.md`).
2. Merge. On every cluster, the fan-out ApplicationSets create the team's AppProject, namespace,
   quota, NetworkPolicy, workload ApplicationSet, and config ApplicationSet — **zero kubectl**. The
   team's first service goes live when it commits a cluster leaf to its own `<team>-gitops` repo.

## Validation

CI (`.github/workflows/validate.yml`) runs `tools/` on every PR:

- `validate_platform.py` — this repo's consistency: fleet ↔ versions coverage, tenant layout,
  template drift, chart source.
- `check_team_layout.py` — the team-repo checks (leaf layout, `goldenChart` policy + ring order,
  render with the pinned chart); also exposed to team repos via the reusable
  `validate-team-repo.yml`.

## Recovery

Re-running the cluster's Terraform stack reinstalls Argo CD and re-applies the single root
Application; the app-of-apps then rebuilds that cluster's entire platform and tenant workloads from
Git. The only non-Git input is the GitHub App key (read from the secret store).
