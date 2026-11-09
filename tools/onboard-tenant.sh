#!/usr/bin/env bash
# onboard-tenant.sh — scaffold a new tenant in platform-gitops with one command.
# Usage: tools/onboard-tenant.sh <team-name> [github-org]
# Example: tools/onboard-tenant.sh team-gamma
#
# Copies tenants/_template -> tenants/<team>, substitutes TEAM_NAME, registers the team in
# registry/tenants.yaml, and prints the remaining one-time steps (the team's own <team>-gitops repo).
# Scaffolder only: it writes files but applies nothing, and refuses if the tenant already exists.
set -euo pipefail

TEAM="${1:?usage: onboard-tenant.sh <team-name> [github-org]}"
ORG="${2:-imkrupakar}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)" # platform-gitops/
SRC="${ROOT}/tenants/_template"
DST="${ROOT}/tenants/${TEAM}"
REGISTRY="${ROOT}/registry/tenants.yaml"

[ -d "$SRC" ] || { echo "template not found: $SRC" >&2; exit 1; }
[ -d "$DST" ] && { echo "tenant already exists: $DST" >&2; exit 1; }

# 1. Copy the template and substitute TEAM_NAME in file contents.
cp -r "$SRC" "$DST"
rm -f "${DST}/README.md" # the template README documents the template itself, not a live tenant
grep -rl 'TEAM_NAME' "$DST" | while read -r f; do
  sed -i.bak "s/TEAM_NAME/${TEAM}/g" "$f" && rm -f "${f}.bak"
done

# 2. Register the team in registry/tenants.yaml (append if not already present).
if [ -f "$REGISTRY" ] && ! grep -qE "^[[:space:]]*-[[:space:]]*name:[[:space:]]*${TEAM}\$" "$REGISTRY"; then
  cat >> "$REGISTRY" <<EOF
  - name: ${TEAM}
    gitops_repo: https://github.com/${ORG}/${TEAM}-gitops
    namespace: ${TEAM}
EOF
fi

cat <<EOF
Scaffolded tenant '${TEAM}':
  platform-gitops/tenants/${TEAM}/            (AppProject + workload & config ApplicationSets + namespace/quota/netpol)
  platform-gitops/registry/tenants.yaml       (registered)

Next (still one PR in platform-gitops, plus the team's own repo):
  1. Review tenants/${TEAM}/namespace/resourcequota.yaml (sizing) and namespace/networkpolicy.yaml (egress).
  2. Create the team's config repo ${ORG}/${TEAM}-gitops with:
       apps/<svc>/base/values.yaml                                  # common chart values
       apps/<svc>/envs/<env>/<cloud>/<region>/values.yaml           # per-cluster LEAF: image pin +
                                                                    #   goldenChart; its existence opts a
                                                                    #   service into that cluster
       config/envs/<env>/<cloud>/<region>/secretstore.yaml          # TEAM-OWNED ESO SecretStore (the team's
       config/envs/<env>/<cloud>/<region>/serviceaccount.yaml       #   own vault + eso-reader SA; platform
                                                                    #   never creates these)
  3. Open the PR. On every cluster the fan-out ApplicationSets create ${TEAM}'s AppProject, namespace,
     ResourceQuota, NetworkPolicy, and the workload + config ApplicationSets — zero kubectl.
     ${TEAM}'s RBAC comes from its AppProject roles (no argocd-rbac-cm change).
  4. Validate the team repo layout:  tools/check_team_layout.py  (also runs on every team-repo PR)
EOF
