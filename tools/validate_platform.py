#!/usr/bin/env python3
"""Validate platform-gitops consistency before a change can merge (every cluster pulls `main`).

Checks
  1. fleet.yaml <-> environments/<env>/<cloud>/<region>/versions.yaml, both directions; labels match
     the path; `addons` keys equal the add-on list in appsets/addons.yaml; no `goldenChart` (team-owned)
  2. every tenant (and _template) has controlplane/{appset,config-appset,project}.yaml and namespace/
     (the SecretStore is team-owned: <team>-gitops/config/envs/<env>/<cloud>/<region>/)
  3. each tenant's workload AppSet, config AppSet and AppProject equal the _template (modulo the team name):
     no silent drift between the template and what is deployed
  4. the golden chart source is consistent: AppSet repoURL == the Argo CD repository Secret url ==
     an entry in every tenant AppProject's sourceRepos, and the missing-pin safe default is present
  5. every ApplicationSet uses goTemplate with missingkey=error; every YAML file parses
  6. each config/<name>/ folder contains manifests

Usage: platform-gitops/tools/validate_platform.py [--fleet FILE]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)
ROOT = os.path.dirname(PLATFORM)
SAFE_DEFAULT = "MISSING-goldenChart-in-leaf"


def docs(path):
    """All YAML documents in a file (yq prints one JSON object per line)."""
    r = subprocess.run(["yq", "-o=json", "-I=0", ".", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"{path}: not valid YAML ({r.stderr.strip().splitlines()[0] if r.stderr.strip() else '?'})")
    return [json.loads(l) for l in r.stdout.splitlines() if l.strip() and l.strip() != "null"]


def doc(path):
    d = docs(path)
    if len(d) != 1:
        raise ValueError(f"{path}: expected exactly one YAML document, found {len(d)}")
    return d[0]


def normalized(path, team):
    """Non-comment, non-blank lines with the team name substituted, for drift comparison."""
    out = []
    for line in open(path):
        t = re.sub(r"\s+#.*$", "", line.rstrip())  # drop inline comments too
        if not t.strip() or t.lstrip().startswith("#"):
            continue
        out.append(t.replace("TEAM_NAME", team))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fleet", default=os.path.join(ROOT, "cluster-platform-terraform", "fleet.yaml"))
    args = ap.parse_args()
    errors = []

    def err(msg):
        errors.append(msg)

    # ---- 5a. everything parses
    for p in sorted(glob.glob(os.path.join(PLATFORM, "**", "*.y*ml"), recursive=True)):
        rel = os.path.relpath(p, PLATFORM)
        if rel.startswith((".github", "tools")):
            continue
        try:
            docs(p)
        except ValueError as e:
            err(str(e))

    try:
        fleet = doc(args.fleet)["clusters"]
    except (ValueError, KeyError) as e:
        sys.exit(f"cannot read fleet: {e}")
    clouds = sorted({c["cloud"] for c in fleet})
    fleet_keys = {(c["environment"], c["cloud"], c["region"]) for c in fleet}

    # ---- 1. versions files
    try:
        addon_list = doc(os.path.join(PLATFORM, "appsets", "addons.yaml"))["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        addon_names = {e["name"] for e in addon_list}
    except (ValueError, KeyError, IndexError) as e:
        addon_names = set()
        err(f"appsets/addons.yaml: cannot read the add-on list ({e})")
    found = set()
    for p in glob.glob(os.path.join(PLATFORM, "environments", "*", "*", "*", "versions.yaml")):
        parts = p.split(os.sep)
        key = (parts[-4], parts[-3], parts[-2])
        found.add(key)
        rel = os.path.relpath(p, PLATFORM)
        if key not in fleet_keys:
            err(f"{rel}: no such cluster in fleet.yaml (orphan versions file)")
            continue
        try:
            v = doc(p)
        except ValueError as e:
            err(str(e))
            continue
        for label, want in zip(("environment", "cloud", "region"), key):
            if v.get(label) != want:
                err(f"{rel}: {label} is {v.get(label)!r} but the path says {want!r}")
        if "goldenChart" in v:
            err(f"{rel}: goldenChart is team-owned now (pinned in each cluster leaf), remove it here")
        pins = v.get("addons") or {}
        if set(pins) != addon_names:
            err(f"{rel}: addons {sorted(pins)} != appsets/addons.yaml {sorted(addon_names)}")
        for k, val in pins.items():
            if not isinstance(val, str) or not val:
                err(f"{rel}: addons.{k} must be a non-empty quoted version string")
    for key in sorted(fleet_keys - found):
        err(f"environments/{'/'.join(key)}/versions.yaml missing: that cluster would render no add-ons")

    # ---- 2. tenants
    tenants = sorted(d for d in glob.glob(os.path.join(PLATFORM, "tenants", "*")) if os.path.isdir(d))
    for t in tenants:
        name = os.path.basename(t)
        need = ["controlplane/appset.yaml", "controlplane/config-appset.yaml", "controlplane/project.yaml",
                "namespace/namespace.yaml", "namespace/resourcequota.yaml", "namespace/networkpolicy.yaml"]
        for f in need:
            if not os.path.isfile(os.path.join(t, f)):
                err(f"tenants/{name}/{f} missing")

    # ---- 3. drift against the template
    tpl = os.path.join(PLATFORM, "tenants", "_template", "controlplane")
    for t in tenants:
        name = os.path.basename(t)
        if name == "_template":
            continue
        for f in ("appset.yaml", "config-appset.yaml", "project.yaml"):
            actual, template = os.path.join(t, "controlplane", f), os.path.join(tpl, f)
            if not (os.path.isfile(actual) and os.path.isfile(template)):
                continue
            a, b = normalized(actual, name), normalized(template, name)
            if a != b:
                diff = next((f"  tenant: {x.strip()}\n  template: {y.strip()}" for x, y in zip(a, b) if x != y), "  (different length)")
                err(f"tenants/{name}/controlplane/{f} drifted from _template:\n{diff}")

    # ---- 4. golden chart source
    repos, urls = {}, set()
    for t in tenants:
        name = os.path.basename(t)
        p = os.path.join(t, "controlplane", "appset.yaml")
        if not os.path.isfile(p):
            continue
        try:
            src = doc(p)["spec"]["template"]["spec"]["sources"][0]
        except (ValueError, KeyError, IndexError) as e:
            err(f"tenants/{name}/controlplane/appset.yaml: cannot read the chart source ({e})")
            continue
        urls.add(src.get("repoURL"))
        if src.get("chart") != "golden-service":
            err(f"tenants/{name}: first source must be the golden-service chart")
        if SAFE_DEFAULT not in str(src.get("targetRevision")):
            err(f"tenants/{name}: targetRevision lacks the {SAFE_DEFAULT} safe default (a leaf without goldenChart would break the whole AppSet)")
        proj = os.path.join(t, "controlplane", "project.yaml")
        if os.path.isfile(proj):
            try:
                if src.get("repoURL") not in (doc(proj)["spec"].get("sourceRepos") or []):
                    err(f"tenants/{name}/controlplane/project.yaml: sourceRepos does not allow the chart repo {src.get('repoURL')}")
            except (ValueError, KeyError):
                pass
    if len(urls) > 1:
        err(f"tenant AppSets disagree on the chart repoURL: {sorted(u for u in urls if u)}")
    secret = os.path.join(PLATFORM, "argocd", "base", "repo-golden-charts.yaml")
    try:
        s = doc(secret)["stringData"]
        if urls and s.get("url") not in urls:
            err(f"argocd/base/repo-golden-charts.yaml url {s.get('url')!r} != AppSet chart repoURL {sorted(urls)}")
        if s.get("enableOCI") != "true" or s.get("type") != "helm":
            err("argocd/base/repo-golden-charts.yaml must be type: helm with enableOCI: \"true\"")
    except (ValueError, KeyError):
        err("argocd/base/repo-golden-charts.yaml missing or unreadable: OCI chart sources must be registered with Argo CD")
    kust = os.path.join(PLATFORM, "argocd", "base", "kustomization.yaml")
    if os.path.isfile(kust) and "repo-golden-charts.yaml" not in open(kust).read():
        err("argocd/base/kustomization.yaml does not include repo-golden-charts.yaml")

    # ---- 5b. ApplicationSet hygiene
    for p in sorted(glob.glob(os.path.join(PLATFORM, "**", "*.y*ml"), recursive=True)):
        rel = os.path.relpath(p, PLATFORM)
        if rel.startswith((".github", "tools")):
            continue
        try:
            ds = docs(p)
        except ValueError:
            continue
        for d in ds:
            if isinstance(d, dict) and d.get("kind") == "ApplicationSet":
                spec = d.get("spec", {})
                if not spec.get("goTemplate") or "missingkey=error" not in (spec.get("goTemplateOptions") or []):
                    err(f"{rel}: ApplicationSet must set goTemplate: true and goTemplateOptions [\"missingkey=error\"]")

    # ---- 6. config folders
    for c in sorted(glob.glob(os.path.join(PLATFORM, "config", "*"))):
        if os.path.isdir(c) and not glob.glob(os.path.join(c, "*.y*ml")):
            err(f"{os.path.relpath(c, PLATFORM)}: empty config folder (its Application would sync nothing)")

    if errors:
        print("PLATFORM-GITOPS PROBLEMS:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print(f"platform-gitops OK: {len(fleet)} clusters, {len(tenants) - 1} tenants + template, add-ons {sorted(addon_names)}, clouds {clouds}")


if __name__ == "__main__":
    main()
