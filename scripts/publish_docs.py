"""Merge a built docs site into the versioned GitHub Pages tree.

Layout of the Pages tree (kept on the ``gh-pages`` branch):

    index.html      redirect to latest/ (latest/ is the default)
    404.html        sends old links (/usage/..., /stable/...) to latest/
    versions.json   read by the theme's version selector (mike format)
    latest/         docs of the latest stable release
    pre/            docs of the latest pre-release (only while it is newer than latest)
    1.1.0a1/, ...   docs of each tagged release (``--versioned``); never removed

Usage:
    python scripts/publish_docs.py --site site --pages gh-pages \\
        --channel latest --release 1.1.0 --versioned --base-path /quantum-as-a-service/

``--channel none --versioned`` publishes only the release folder, e.g. to add the
docs of an older tag without changing latest/ or pre/.

Docs are published for releases from 1.0 on (the ``qheappe_client`` API).
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from packaging.version import Version

CHANNELS = {
    "latest": "{release} (latest stable)",
    "pre": "{release} (pre-release)",
}
NO_CHANNEL = "none"
DEFAULT = "latest"
# Folders of earlier layouts and the channel that replaced them.
RENAMED = {"stable": "latest"}
# Oldest release with published docs.
MIN_MAJOR = 1

REDIRECT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Redirecting…</title>
  <link rel="canonical" href="{default}/">
  <meta http-equiv="refresh" content="0; url={default}/">
  <script>location.replace("{default}/" + location.search + location.hash);</script>
</head>
<body><a href="{default}/">Go to the documentation</a></body>
</html>
"""

# GitHub Pages serves this for every missing path. Unversioned links from before
# versioning (e.g. /quantum-as-a-service/usage/) go to latest/; links into a renamed
# folder (e.g. /quantum-as-a-service/stable/usage/) go to the folder that replaced it.
NOT_FOUND_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Page not found</title>
  <script>
    (function () {{
      var base = {base!r};
      var folders = {folders};
      var renamed = {renamed};
      var path = location.pathname;
      if (path.indexOf(base) !== 0) return;
      var rest = path.slice(base.length);
      var first = rest.split("/")[0];
      if (renamed[first]) {{
        rest = renamed[first] + rest.slice(first.length);
      }} else if (folders.indexOf(first) !== -1) {{
        return;
      }} else {{
        rest = {default!r} + "/" + rest;
      }}
      location.replace(base + rest + location.search + location.hash);
    }})();
  </script>
</head>
<body>
  <h1>Page not found</h1>
  <p><a href="{base}{default}/">Go to the documentation</a></p>
</body>
</html>
"""


def load_versions(pages: Path) -> list[dict]:
    path = pages / "versions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def release_of(entry: dict) -> Version:
    return Version(entry.get("properties", {}).get("release", "0"))


def replace_folder(site: Path, target: Path) -> None:
    """Replace ``target`` with a copy of the built ``site``."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(site, target)


def channel_entry(channel: str, release: str) -> dict:
    return {
        "version": channel,
        "title": CHANNELS[channel].format(release=release),
        "aliases": [],
        "properties": {"release": release},
    }


def migrate(pages: Path, versions: list[dict]) -> list[dict]:
    """Rename folders of earlier layouts, e.g. stable/ -> latest/."""
    for old, new in RENAMED.items():
        entry = next((v for v in versions if v["version"] == old), None)
        if entry is None and not (pages / old).exists():
            continue
        if (pages / old).exists():
            if (pages / new).exists():
                shutil.rmtree(pages / old)
            else:
                (pages / old).rename(pages / new)
        versions = [v for v in versions if v["version"] != old]
        if entry is not None and not any(v["version"] == new for v in versions):
            versions.append(channel_entry(new, str(release_of(entry))))
        print(f"Renamed {old}/ to {new}/")
    return versions


def ordered(versions: list[dict]) -> list[dict]:
    """Channels first (latest, pre), then release folders, newest first."""
    channels = sorted(
        (v for v in versions if v["version"] in CHANNELS),
        key=lambda v: list(CHANNELS).index(v["version"]),
    )
    releases = sorted(
        (v for v in versions if v["version"] not in CHANNELS),
        key=release_of,
        reverse=True,
    )
    return channels + releases


def publish(
    site: Path, pages: Path, channel: str, release: str, base: str, versioned: bool
) -> None:
    if channel != NO_CHANNEL and channel not in CHANNELS:
        raise SystemExit(
            f"Unknown channel {channel!r}, expected one of {[*CHANNELS, NO_CHANNEL]}"
        )
    if channel == NO_CHANNEL and not versioned:
        raise SystemExit("Nothing to publish: use a channel, --versioned, or both")
    version = Version(release)
    if version.major < MIN_MAJOR:
        raise SystemExit(
            f"{release}: docs are published for releases from {MIN_MAJOR}.0 on only"
        )
    if channel == DEFAULT and version.is_prerelease:
        raise SystemExit(
            f"{release} is a pre-release and cannot be published as {DEFAULT}"
        )

    pages.mkdir(parents=True, exist_ok=True)
    versions = migrate(pages, load_versions(pages))

    if versioned:
        # Release folder, e.g. 1.1.0a1/; kept for good, replaced only by a rebuild.
        folder = str(version)
        replace_folder(site, pages / folder)
        versions = [v for v in versions if v["version"] != folder]
        versions.append(
            {
                "version": folder,
                "title": folder,
                "aliases": [],
                "properties": {"release": folder},
            }
        )

    if channel != NO_CHANNEL:
        # Replace the channel folder with the new build.
        replace_folder(site, pages / channel)
        versions = [v for v in versions if v["version"] != channel]
        versions.append(channel_entry(channel, release))

    # Once latest catches up with the pre-release, the pre-release docs are stale.
    latest = next((v for v in versions if v["version"] == DEFAULT), None)
    pre = next((v for v in versions if v["version"] == "pre"), None)
    if latest and pre and release_of(pre) <= release_of(latest):
        print(
            f"Removing pre/ ({release_of(pre)}): not newer than {DEFAULT} "
            f"({release_of(latest)})"
        )
        versions.remove(pre)
        shutil.rmtree(pages / "pre", ignore_errors=True)

    versions = ordered(versions)
    (pages / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
    (pages / "index.html").write_text(REDIRECT_HTML.format(default=DEFAULT))
    (pages / "404.html").write_text(
        NOT_FOUND_HTML.format(
            base=base,
            default=DEFAULT,
            folders=json.dumps([v["version"] for v in versions]),
            renamed=json.dumps(RENAMED),
        )
    )
    (pages / ".nojekyll").touch()

    for v in versions:
        print(f"  {v['version']:<10} {v['title']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--site", type=Path, required=True, help="built site directory")
    parser.add_argument(
        "--pages", type=Path, required=True, help="checkout of the gh-pages branch"
    )
    parser.add_argument(
        "--channel",
        required=True,
        choices=[*CHANNELS, NO_CHANNEL],
        help="channel folder to replace, or 'none' (only with --versioned)",
    )
    parser.add_argument(
        "--release", required=True, help="package version of the build, e.g. 1.1.0a1"
    )
    parser.add_argument(
        "--versioned",
        action="store_true",
        help="also publish the build to its own release folder, e.g. 1.1.0a1/",
    )
    parser.add_argument(
        "--base-path", default="/", help="URL path of the Pages site, e.g. /repo/"
    )
    args = parser.parse_args()

    base = "/" + args.base_path.strip("/") + "/" if args.base_path.strip("/") else "/"
    publish(args.site, args.pages, args.channel, args.release, base, args.versioned)


if __name__ == "__main__":
    main()
