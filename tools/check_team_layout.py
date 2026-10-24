#!/usr/bin/env python3
"""Check a team's GitOps repo before its PR can merge: catch mistakes and prove the config still renders.

WHAT THIS GUARDS AGAINST
  Teams describe what to deploy with layered values.yaml files. Small mistakes here (a typo'd region,
  a chart version that was never soak-tested, an image pinned in the wrong place) would ship straight
  to a cluster. This script runs in CI and fails the PR before that can happen.

HOW A SERVICE IS DESCRIBED  (files under <team>-gitops/apps/<svc>/)
  Settings are layered from general to specific; a more specific file overrides a less specific one:
    base/values.yaml                             defaults for every cluster
    envs/<env>/values.yaml                       whole-environment settings, e.g. sbx     (optional)
    envs/<env>/<cloud>/values.yaml               whole-cloud settings, e.g. azure         (optional)
    envs/<env>/<cloud>/<region>/values.yaml      the "LEAF": one specific cluster
  The leaf is special: its existence is what turns the service ON for that cluster, and it carries the
  exact image to run (tag + digest) and the chart version to deploy with (`goldenChart`).

WHAT IT CHECKS ON EVERY LEAF
  - the cluster is real: the <env>/<cloud>/<region> matches an entry in fleet.yaml (catches typos
    like `us-east1` instead of `us-east-2`)
  - the image (tag + digest) is pinned ONLY in the leaf, and the digest is a real sha256:... value
  - the chart version `goldenChart` is a plain SemVer like "2.7.0" and is allowed by the support
    policy: not older than the minimum supported, not newer than the latest, warned if deprecated
  - rings are promoted in order: a cluster may not run a newer chart than the ring below it, so every
    version soaks in a lower environment first  (ring order: sbx -> dev -> stg -> prd)
  - the leaf doesn't use reserved top-level keys that would clash with the deployment machinery
  - it actually renders: `helm template` of the leaf's pinned chart + its layered values succeeds
    (pass --chart-oci or --chart-packages to use the published chart; otherwise it falls back to the
    local chart and says so)
  - the image comes from the right registry: the host matches the cloud, and prd never pulls from a
    non-prod registry

HOW TO RUN IT
  check everything in the monorepo:   platform-gitops/tools/check_team_layout.py
  check one team repo (as CI does):   check_team_layout.py --team-repo . --fleet fleet.yaml \
                                        --chart-dir charts/golden-service --policy support-policy.yaml \
                                        --chart-oci oci://ghcr.io/krupakar-hyland/charts/golden-service
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # the folder that holds all the repos
HOST_SUFFIX = {"azure": ".azurecr.io", "aws": ".amazonaws.com"}
IMAGE_RE = re.compile(r'image:\s*"?([^/"\s]+)/')
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
RING = ["sbx", "dev", "stg", "prd"]
# keys the workload ApplicationSet already uses as generator params; a leaf must not reuse them
RESERVED_KEYS = {"name", "server", "metadata", "environment", "cloud", "region", "addons"}


class BadYaml(Exception):
    """A file that cannot be read as one YAML document (reported as a normal finding, never a traceback)."""


def load(path):
    # yq keeps this script dependency-free (it is already required by the CI/promote workflows).
    r = subprocess.run(["yq", "-o=json", ".", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise BadYaml(f"{path}: not valid YAML ({r.stderr.strip().splitlines()[0] if r.stderr.strip() else '?'})")
    try:
        return json.loads(r.stdout) or {}
    except ValueError:
        raise BadYaml(f"{path}: must be a single YAML document (found several)")


def ver(v):
    m = SEMVER_RE.match(str(v))
    return tuple(int(x) for x in m.groups()) if m else None


def leaves(team_repo):
    pattern = os.path.join(team_repo, "apps", "*", "envs", "*", "*", "*", "values.yaml")
    for path in sorted(glob.glob(pattern)):
        parts = path.split(os.sep)
        yield path, parts[-6], parts[-4], parts[-3], parts[-2]  # leaf, svc, env, cloud, region


def chain(app_dir, env, cloud, region):
    """Existing files only, in the same order the ApplicationSet lists them."""
    candidates = [
        os.path.join(app_dir, "base", "values.yaml"),
        os.path.join(app_dir, "envs", env, "values.yaml"),
        os.path.join(app_dir, "envs", env, cloud, "values.yaml"),
        os.path.join(app_dir, "envs", env, cloud, region, "values.yaml"),
    ]
    return [p for p in candidates if os.path.isfile(p)]


class ChartSource:
    """Resolve a pinned chart version to a chart directory (OCI pull, packaged tgz, or the local chart)."""

    def __init__(self, local_dir, oci, packages):
        self.local, self.oci, self.packages = local_dir, oci, packages
        self.cache, self.tmp = {}, tempfile.mkdtemp(prefix="golden-chart-")
        self.fallback_leaves = 0

    @property
    def pinned_mode(self):
        return bool(self.oci or self.packages)

    def get(self, version):
        """Return (chart_dir, error)."""
        if not self.pinned_mode:
            self.fallback_leaves += 1
            return self.local, None
        if version in self.cache:
            return self.cache[version]
        dest = os.path.join(self.tmp, version)
        os.makedirs(dest, exist_ok=True)
        if self.oci:
            r = subprocess.run(["helm", "pull", self.oci, "--version", version, "--untar", "--untardir", dest],
                               capture_output=True, text=True)
            if r.returncode != 0:
                res = (None, f"cannot pull golden-service {version} from {self.oci}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else '?'}")
                self.cache[version] = res
                return res
        else:
            tgz = os.path.join(self.packages, f"golden-service-{version}.tgz")
            if not os.path.isfile(tgz):
                res = (None, f"no packaged chart golden-service-{version}.tgz in {self.packages}")
                self.cache[version] = res
                return res
            with tarfile.open(tgz) as t:
                t.extractall(dest)
        res = (os.path.join(dest, "golden-service"), None)
        self.cache[version] = res
        return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=ROOT, help="folder holding all repos (monorepo mode)")
    ap.add_argument("--team-repo", action="append", help="a team GitOps repo to check (repeatable); default: <root>/*-gitops")
    ap.add_argument("--team-name", help="namespace for rendering; default: repo folder name minus -gitops")
    ap.add_argument("--fleet", help="fleet.yaml; default <root>/cluster-platform-terraform/fleet.yaml")
    ap.add_argument("--chart-dir", help="local golden-service chart; default <root>/golden-helm-charts/charts/golden-service")
    ap.add_argument("--policy", help="support-policy.yaml; default <root>/golden-helm-charts/support-policy.yaml")
    ap.add_argument("--chart-oci", help="oci://.../golden-service: render each leaf with its pinned version")
    ap.add_argument("--chart-packages", help="dir of golden-service-X.tgz: like --chart-oci, offline")
    ap.add_argument("--out", help="write rendered manifests here (one file per team/svc/cluster)")
    args = ap.parse_args()

    fleet_path = args.fleet or os.path.join(args.root, "cluster-platform-terraform", "fleet.yaml")
    chart_dir = args.chart_dir or os.path.join(args.root, "golden-helm-charts", "charts", "golden-service")
    policy_path = args.policy or os.path.join(args.root, "golden-helm-charts", "support-policy.yaml")
    teams = args.team_repo or sorted(glob.glob(os.path.join(args.root, "*-gitops")))
    teams = [t for t in teams if os.path.isdir(os.path.join(t, "apps"))]
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    try:
        clusters = {(c["environment"], c["cloud"], c["region"]) for c in load(fleet_path)["clusters"]}
        policy = load(policy_path)
    except (BadYaml, KeyError) as e:
        sys.exit(f"cannot read fleet/policy: {e}")
    pmin, platest, deprecated = ver(policy["minimumSupported"]), ver(policy["latest"]), set(policy.get("deprecated") or [])
    charts = ChartSource(chart_dir, args.chart_oci, args.chart_packages)
    errors, warnings, count = [], [], 0

    for team_repo in teams:
        repo_name = os.path.basename(os.path.abspath(team_repo))
        team = args.team_name or repo_name.removesuffix("-gitops")
        pins = {}  # (svc, env) -> [version tuples], for the soak-order check
        for leaf, svc, env, cloud, region in leaves(team_repo):
            count += 1
            tag = f"{repo_name}/{svc} {env}/{cloud}/{region}"
            app_dir = os.path.join(team_repo, "apps", svc)

            if (env, cloud, region) not in clusters:
                errors.append(f"{tag}: no such cluster in fleet.yaml")
                continue

            files = chain(app_dir, env, cloud, region)
            try:
                leaf_doc = load(leaf)
                upper = {p: load(p) for p in files[:-1]}
            except BadYaml as e:
                errors.append(f"{tag}: {e}")
                continue

            # --- image pin lives in the leaf only
            for p, doc in upper.items():
                for k in {"tag", "digest"} & set(doc.get("image") or {}):
                    errors.append(f"{tag}: image.{k} must live in the leaf, found in {os.path.relpath(p, team_repo)}")
            pin = leaf_doc.get("image") or {}
            unpinned_sbx = False
            if not pin.get("tag") or not str(pin.get("digest", "")).startswith("sha256:"):
                if env == "sbx" and not pin.get("tag") and not pin.get("digest"):
                    # a brand-new service: its sbx leaf exists before the first CI build, which writes the pin
                    unpinned_sbx = True
                    warnings.append(f"{tag}: no image pin yet; the first CI build writes it (the sbx Application errors until then)")
                else:
                    errors.append(f"{tag}: leaf must set image.tag and image.digest (sha256:...)")

            # --- golden chart pin (team-owned, so it is policed here)
            gc = leaf_doc.get("goldenChart")
            gcv = ver(gc) if isinstance(gc, str) else None
            if gc is None or gc == "":
                errors.append(f"{tag}: leaf must pin `goldenChart` (e.g. goldenChart: \"{policy['latest']}\")")
            elif not isinstance(gc, str) or gcv is None:
                errors.append(f"{tag}: goldenChart {gc!r} must be a quoted plain SemVer string like \"{policy['latest']}\"")
            else:
                if gcv < pmin:
                    errors.append(f"{tag}: goldenChart {gc} is below the minimum supported {policy['minimumSupported']}")
                if gcv > platest:
                    errors.append(f"{tag}: goldenChart {gc} is newer than the latest released {policy['latest']} (typo or unpublished)")
                if gc in deprecated:
                    warnings.append(f"{tag}: goldenChart {gc} is deprecated, plan an upgrade")
                pins.setdefault((svc, env), []).append(gcv)
            bad = RESERVED_KEYS & set(leaf_doc)
            if bad:
                errors.append(f"{tag}: top-level key(s) {sorted(bad)} collide with ApplicationSet generator params")

            # --- render with the pinned chart + the params the ApplicationSet passes
            chart, err = charts.get(gc if gcv else policy["latest"])
            if err:
                errors.append(f"{tag}: {err}")
                continue
            cmd = ["helm", "template", svc, chart, "-n", team]
            for p in files:
                cmd += ["-f", p]
            cmd += ["--set", f"global.cloud={cloud},global.region={region},global.environment={env}"]
            if unpinned_sbx:
                cmd += ["--set", "image.tag=pending-first-build"]  # lets the rest of the leaf be validated
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                errors.append(f"{tag}: helm template failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else '?'}")
                continue
            if args.out:
                with open(os.path.join(args.out, f"{repo_name}.{svc}.{env}.{cloud}.{region}.yaml"), "w") as f:
                    f.write(r.stdout)

            m = IMAGE_RE.search(r.stdout)
            host = m.group(1) if m else ""
            if not host.endswith(HOST_SUFFIX.get(cloud, "")):
                errors.append(f"{tag}: image host '{host}' does not match cloud '{cloud}'")
            if env == "prd" and "nonprod" in host:
                errors.append(f"{tag}: prd must not pull from the nonprod registry ('{host}')")

        # --- soak order: a higher ring may not run a chart newer than the ring below it
        for (svc, env), vs in sorted(pins.items()):
            lower = [e for e in RING[: RING.index(env)] if (svc, e) in pins] if env in RING else []
            if not lower:
                continue
            below = max(max(pins[(svc, lower[-1])]), (0, 0, 0))
            if max(vs) > below:
                errors.append(f"{repo_name}/{svc} {env}: goldenChart {'.'.join(map(str, max(vs)))} is newer than anything in {lower[-1]} "
                              f"({'.'.join(map(str, below))}); upgrade the lower ring first")

    if not charts.pinned_mode and charts.fallback_leaves:
        warnings.append(f"{charts.fallback_leaves} leaf render(s) used the local chart, not each leaf's pinned goldenChart "
                        f"(pass --chart-oci or --chart-packages to test the real pins)")
    for w in warnings:
        print("WARN:", w)
    if errors:
        print("TEAM LAYOUT PROBLEMS:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print(f"team layout OK: {count} cluster leaves pass the layout, goldenChart and render checks ({len(warnings)} warning(s))")


if __name__ == "__main__":
    main()
