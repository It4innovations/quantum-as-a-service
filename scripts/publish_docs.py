"""Merge a built docs site into the versioned GitHub Pages tree.

Layout of the Pages tree (kept on the ``gh-pages`` branch):

    index.html      redirect to stable/ (stable/ is the default)
    404.html        sends old unversioned links (/usage/...) to stable/
    versions.json   read by the theme's version selector (mike format)
    stable/         docs of the latest stable release
    pre/            docs of the latest pre-release (only while it is newer than stable)

Usage:
    python scripts/publish_docs.py --site site --pages gh-pages \
        --channel stable --release 1.0.0 --base-path /quantum-as-a-service/
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from packaging.version import Version

CHANNELS = {
    "stable": "{release} (stable)",
    "pre": "{release} (pre-release)",
}

REDIRECT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Redirecting…</title>
  <link rel="canonical" href="stable/">
  <meta http-equiv="refresh" content="0; url=stable/">
  <script>location.replace("stable/" + location.search + location.hash);</script>
</head>
<body><a href="stable/">Go to the documentation</a></body>
</html>
"""

# GitHub Pages serves this for every missing path. Links from before versioning
# (e.g. /quantum-as-a-service/usage/) are redirected into stable/.
NOT_FOUND_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Page not found</title>
  <script>
    (function () {{
      var base = {base!r};
      var channels = {channels};
      var path = location.pathname;
      if (path.indexOf(base) !== 0) return;
      var rest = path.slice(base.length);
      if (channels.indexOf(rest.split("/")[0]) !== -1) return;
      location.replace(base + "stable/" + rest + location.search + location.hash);
    }})();
  </script>
</head>
<body>
  <h1>Page not found</h1>
  <p><a href="{base}stable/">Go to the documentation</a></p>
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


def publish(site: Path, pages: Path, channel: str, release: str, base: str) -> None:
    if channel not in CHANNELS:
        raise SystemExit(
            f"Unknown channel {channel!r}, expected one of {list(CHANNELS)}"
        )
    version = Version(release)
    if channel == "stable" and version.is_prerelease:
        raise SystemExit(
            f"{release} is a pre-release and cannot be published as stable"
        )

    pages.mkdir(parents=True, exist_ok=True)

    # Replace the channel folder with the new build.
    target = pages / channel
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(site, target)

    versions = [v for v in load_versions(pages) if v["version"] != channel]
    versions.append(
        {
            "version": channel,
            "title": CHANNELS[channel].format(release=release),
            "aliases": [],
            "properties": {"release": release},
        }
    )

    # Once stable catches up with the pre-release, the pre-release docs are stale.
    stable = next((v for v in versions if v["version"] == "stable"), None)
    pre = next((v for v in versions if v["version"] == "pre"), None)
    if stable and pre and release_of(pre) <= release_of(stable):
        print(
            f"Removing pre/ ({release_of(pre)}): not newer than stable ({release_of(stable)})"
        )
        versions.remove(pre)
        shutil.rmtree(pages / "pre", ignore_errors=True)

    versions.sort(key=lambda v: list(CHANNELS).index(v["version"]))
    (pages / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
    (pages / "index.html").write_text(REDIRECT_HTML)
    (pages / "404.html").write_text(
        NOT_FOUND_HTML.format(base=base, channels=json.dumps(list(CHANNELS)))
    )
    (pages / ".nojekyll").touch()

    for v in versions:
        print(f"  {v['version']:<7} {v['title']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--site", type=Path, required=True, help="built site directory")
    parser.add_argument(
        "--pages", type=Path, required=True, help="checkout of the gh-pages branch"
    )
    parser.add_argument("--channel", required=True, choices=list(CHANNELS))
    parser.add_argument(
        "--release", required=True, help="package version of the build, e.g. 1.1.0a1"
    )
    parser.add_argument(
        "--base-path", default="/", help="URL path of the Pages site, e.g. /repo/"
    )
    args = parser.parse_args()

    base = "/" + args.base_path.strip("/") + "/" if args.base_path.strip("/") else "/"
    publish(args.site, args.pages, args.channel, args.release, base)


if __name__ == "__main__":
    main()
