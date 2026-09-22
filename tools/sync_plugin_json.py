"""Regenerate plugin.json from plugin.py's Plugin class (name, version,
description, author, license, fields, actions) — the two must stay in
sync since Dispatcharr reads plugin.json for its UI. Run after any change
to Plugin.fields/actions/version/description.

Usage: python3 tools/sync_plugin_json.py
"""
import importlib.util
import json
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def main():
    spec = importlib.util.spec_from_file_location(
        "plugin_module", os.path.join(ROOT, "plugin.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Plugin = module.Plugin

    plugin_json_path = os.path.join(ROOT, "plugin.json")
    with open(plugin_json_path) as f:
        current = json.load(f)

    new = {
        "name": Plugin.name,
        "version": Plugin.version,
        "description": Plugin.description,
        "author": getattr(Plugin, "author", current.get("author")),
        "license": getattr(Plugin, "license", current.get("license")),
        "fields": Plugin.fields,
        "actions": Plugin.actions,
    }
    with open(plugin_json_path, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Synced {plugin_json_path} from plugin.py (version {Plugin.version}).")


if __name__ == "__main__":
    main()
