"""Prepare a Dispatcharr/Plugins registry bump PR for one of our external plugins.

Builds plugins/<slug>/plugin.json (+ logo.png) from a release tag of the
plugin's own repo, adds the external-plugin fields the registry needs,
commits it on a fresh branch cut from upstream/main in your fork checkout,
and prints (or, with --push, runs) the push + PR steps.

Nothing leaves your machine unless you pass --push.

Usage:
    python3 tools/registry_bump.py vod-manager --registry ~/dev/Plugins
    python3 tools/registry_bump.py vod-probe v1.3.3 --registry ~/dev/Plugins --push

The registry checkout is your fork of Dispatcharr/Plugins, with a remote
named `upstream` pointing at Dispatcharr/Plugins (what `gh repo fork
--clone` sets up). It must have a clean working tree.
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REGISTRY_REPO = "Dispatcharr/Plugins"

# slug -> where the source repo lives and what its release asset is called.
PLUGINS = {
    "vod-manager": {
        "path": Path.home() / "Documents/dev/dispatcharr-vod-manager",
        "github": "oxios0x00/dispatcharr-vod-manager",
        "zip": "vod_manager.zip",
        "title": "VOD Manager",
    },
    "vod-probe": {
        "path": Path.home() / "Documents/dev/vod-probe",
        "github": "oxios0x00/dispatcharr-vod-probe",
        "zip": "vod_probe.zip",
        "title": "VOD Probe",
    },
}


def run(args, cwd, check=True, binary=False):
    result = subprocess.run(
        args, cwd=cwd, check=check, capture_output=True, text=not binary
    )
    return result.stdout if binary else result.stdout.strip()


def fail(message):
    sys.exit(f"error: {message}")


def parse_version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        fail(f"'{text}' is not MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def latest_tag(repo):
    tags = run(["git", "tag", "--list", "v*"], repo).split()
    tags = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    if not tags:
        fail(f"no vX.Y.Z tag in {repo}")
    return max(tags, key=parse_version)


def asset_reachable(url):
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except urllib.error.URLError:
        return False


def build_manifest(source, github, zip_name):
    """Source plugin.json minus fields/actions, plus the registry's
    external-plugin fields, inserted right after `license` to keep the
    field order stable."""
    repo_url = f"https://github.com/{github}"
    external = {
        "help_url": repo_url,
        "repo_url": repo_url,
        "source_type": "external",
        "source_url": f"{repo_url}/releases/download/v{{version}}/{zip_name}",
    }
    out = {}
    for key, value in source.items():
        # The registry entry is metadata only; the settings UI and actions
        # come from the plugin.json inside the released zip.
        if key in external or key in ("fields", "actions"):
            continue
        out[key] = value
        if key == "license":
            out.update(external)
    if "license" not in source:
        fail("plugin.json has no license field")
    return out


def published_version(registry, slug):
    path = f"upstream/main:plugins/{slug}/plugin.json"
    proc = subprocess.run(
        ["git", "show", path], cwd=registry, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout).get("version")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plugin", choices=sorted(PLUGINS))
    parser.add_argument("tag", nargs="?", help="release tag (default: latest vX.Y.Z)")
    parser.add_argument("--registry", required=True, type=Path,
                        help="checkout of your Dispatcharr/Plugins fork")
    parser.add_argument("--repo", type=Path,
                        help="source repo (default: known location per plugin)")
    parser.add_argument("--push", action="store_true",
                        help="push the branch and open the PR (otherwise only commit locally)")
    parser.add_argument("--no-version-check", action="store_true",
                        help="skip the must-be-greater-than-published check (testing)")
    args = parser.parse_args()

    cfg = PLUGINS[args.plugin]
    repo = (args.repo or cfg["path"]).expanduser()
    registry = args.registry.expanduser()
    for label, path in (("source repo", repo), ("registry checkout", registry)):
        if not (path / ".git").exists():
            fail(f"{label} not found: {path}")

    tag = args.tag or latest_tag(repo)
    version = tag.lstrip("v")
    parse_version(version)
    if run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], repo, check=False) == "":
        fail(f"tag {tag} does not exist in {repo}")

    source = json.loads(run(["git", "show", f"{tag}:plugin.json"], repo))
    if source.get("version") != version:
        fail(f"plugin.json at {tag} says version {source.get('version')}, expected {version}")

    zip_url = f"https://github.com/{cfg['github']}/releases/download/{tag}/{cfg['zip']}"
    if not asset_reachable(zip_url):
        fail(f"release asset not reachable: {zip_url}\n"
             "Publish the GitHub release with its zip before opening the registry PR.")

    if run(["git", "status", "--porcelain"], registry):
        fail(f"registry checkout has uncommitted changes: {registry}")
    run(["git", "fetch", "-q", "upstream"], registry)

    current = published_version(registry, args.plugin)
    if current and not args.no_version_check:
        if parse_version(version) <= parse_version(current):
            fail(f"{version} is not greater than the published {current}")
    action = "Bump version to" if current else "Add"
    verb_title = f"{action} {version}" if current else f"Add {cfg['title']} {version}"

    branch = f"{args.plugin}-{version}"
    run(["git", "checkout", "-q", "-B", branch, "upstream/main"], registry)

    folder = registry / "plugins" / args.plugin
    folder.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(source, cfg["github"], cfg["zip"])
    (folder / "plugin.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    logo = run(["git", "show", f"{tag}:logo.png"], repo, check=False, binary=True)
    if logo:
        (folder / "logo.png").write_bytes(logo)
    else:
        print(f"warning: no logo.png at {tag}; the registry entry has no icon")

    run(["git", "add", f"plugins/{args.plugin}"], registry)
    if not run(["git", "status", "--porcelain"], registry):
        fail("nothing changed against upstream/main")
    title = f"[{args.plugin}] {verb_title}"
    run(["git", "commit", "-q", "-m", title], registry)
    print(f"Committed on branch {branch} in {registry}: {title}")

    owner = re.search(
        r"github\.com[:/]([^/]+)/",
        run(["git", "remote", "get-url", "origin"], registry),
    ).group(1)
    pr_cmd = [
        "gh", "pr", "create", "--repo", REGISTRY_REPO, "--base", "main",
        "--head", f"{owner}:{branch}", "--title", title,
        "--body", f"Bump {cfg['title']} to {version}. External plugin: "
                  f"https://github.com/{cfg['github']}/releases/tag/{tag}",
    ]
    if not args.push:
        print("\nReview with:  git -C", registry, "show --stat HEAD")
        print("Then run again with --push, or by hand:")
        print(f"  git -C {registry} push -u origin {branch}")
        print("  " + " ".join(f"'{a}'" if " " in a else a for a in pr_cmd))
        return
    run(["git", "push", "-u", "origin", branch], registry)
    print(run(pr_cmd, registry))


if __name__ == "__main__":
    main()
