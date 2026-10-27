# Tenant Argo CD Project RBAC

Explains what `tenants/<team>/controlplane/project.yaml` sets up, using the `team-alpha` tenant as
the worked example. The template version with `TEAM_NAME` placeholders lives at
`tenants/_template/controlplane/project.yaml`.

`project.yaml` defines the `team-alpha` AppProject: the tenant boundary (allowed
source repos, destinations, resource kinds) plus the RBAC `roles` that control
who can act on this tenant's apps in the Argo CD UI/CLI.

## Roles and groups

- **`roles`** — named permission sets scoped to this project. Each role has:
  - `policies` — Casbin-style rules: `p, subject, resource, action, object, effect`
  - `groups` — SSO/OIDC group names that get this role automatically on login
- **`groups`** — identity provider group claims (e.g. GitHub team, Dex/OIDC/SAML
  group). Any user whose login carries a listed group is granted that role's
  policies for this project — no manual user management in Argo CD itself.

## The `developer` role

```yaml
roles:
  - name: developer
    description: sync + read for team-alpha apps
    policies:
      - p, proj:team-alpha:developer, applications, get,  team-alpha/team-alpha/*, allow
      - p, proj:team-alpha:developer, applications, sync, team-alpha/team-alpha/*, allow
    groups:
      - krupakar-hyland:team-alpha
```

- Grants **`get`** (view) and **`sync`** on Applications — nothing else
  (no `create`, `delete`, `update`, `override`, or custom `action`).
- Members of the `krupakar-hyland:team-alpha` GitHub team are auto-granted
  this role via SSO group claim.

### Policy object format: `team-alpha/team-alpha/*`

```
<project-name>/<application-namespace>/<application-name-pattern>
```

| Segment | Value | Meaning |
|---|---|---|
| 1st | `team-alpha` | the AppProject this rule applies to |
| 2nd | `team-alpha` | namespace where the `Application` CR lives |
| 3rd | `*` | wildcard — matches any Application name |

So the rule reads: *any Application, of any name, whose Application CR lives
in the `team-alpha` namespace, under the `team-alpha` project.*

This 3-segment form (`project/namespace/name`) is required because
`project.yaml` enables **Applications-in-any-namespace**
(`spec.sourceNamespaces: [team-alpha]`) — team-alpha's `Application` CRs live
in their own namespace rather than the shared `argocd` control-plane
namespace, so the namespace segment disambiguates which apps a rule covers.
(Projects that don't use that feature can use the older 2-segment
`project/name` form, which implicitly means the `argocd` namespace.)

**Net effect:** engineers in `krupakar-hyland:team-alpha` can view and
manually sync team-alpha's own apps — and nothing outside this project.
