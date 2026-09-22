"""Build a release zip containing only the files Dispatcharr needs to run
this plugin — no tests, docs, CI config or dev tooling.

Reads from the git index at the given ref (default HEAD), not the working
directory, so the zip always matches exactly what's committed. Extracting
it into Dispatcharr's plugins directory produces `vod_manager/`, the folder
name Dispatcharr uses as the plugin key.

Usage: python3 tools/build_release_zip.py [ref] [--out PATH]
"""
import argparse
import subprocess
import sys

RUNTIME_FILES = [
    "__init__.py",
    "plugin.py",
    "plugin.json",
    "pipeline.py",
    "schedule.py",
    "store.py",
    "selection.py",
    "measurements.py",
    "exclusions.py",
    "strm.py",
    "title_cleanup.py",
    "LICENSE",
    "README.md",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ref", nargs="?", default="HEAD")
    parser.add_argument("--out", default="vod_manager.zip")
    args = parser.parse_args()

    subprocess.run(
        [
            "git", "archive", "--format=zip", "--prefix=vod_manager/",
            "-o", args.out, args.ref, *RUNTIME_FILES,
        ],
        check=True,
    )
    print(f"Built {args.out} from {args.ref}: {', '.join(RUNTIME_FILES)}")


if __name__ == "__main__":
    sys.exit(main())
