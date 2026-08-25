#!/usr/bin/env python3
"""Build a KiCad Plugin and Content Manager repository from README.md.

Emits ``repository.json``, ``packages.json`` and ``resources.zip`` so that
awesome-kicad can be added to KiCad as a single PCM source.

Only projects that are installable *inside* KiCad are included. A project
qualifies when either:

  * it hosts its own PCM ``packages.json`` at the root of its default branch
    (its entries are merged verbatim), or
  * its latest GitHub release ships a PCM archive, i.e. a ``.zip`` with a
    ``metadata.json`` at the root declaring ``identifier`` and ``versions``.

Everything else -- CLI tools, web apps, FreeCAD workbenches, tutorials -- is
skipped and listed in the generated report.

Only the standard library is used. Set ``GITHUB_TOKEN`` to avoid the very low
unauthenticated API rate limit.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

REPO_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")

# Ignore links that point at this list itself or at the awesome badge.
SKIP_REPOS = {"joanbono/awesome-kicad", "sindresorhus/awesome"}

# Release assets larger than this are never PCM archives; skipping them keeps
# the workflow from pulling down installer images and platform bundles.
MAX_ASSET_BYTES = 64 * 1024 * 1024

VALID_TYPES = {"plugin", "library", "colortheme"}

PACKAGE_SCHEMA = "https://go.kicad.org/pcm/schemas/v1"
REPOSITORY_SCHEMA = "https://go.kicad.org/pcm/schemas/v1#/definitions/Repository"

# Fixed timestamp so an unchanged package set produces a byte-identical
# resources.zip, and therefore a stable sha256.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

USER_AGENT = "awesome-kicad-pcm-builder"


class Http:
    """Minimal HTTP helper with retries and optional GitHub authentication."""

    def __init__(self, token: str | None):
        self.token = token

    def get(self, url: str, *, api: bool = False, allow_404: bool = False) -> bytes | None:
        headers = {"User-Agent": USER_AGENT}
        if api:
            headers["Accept"] = "application/vnd.github+json"
        if self.token and ("api.github.com" in url or "github.com" in url):
            headers["Authorization"] = f"Bearer {self.token}"

        last_error: Exception | None = None
        for attempt in range(4):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code in (403, 404) and allow_404:
                    return None
                if error.code == 403 and "rate limit" in str(error.reason).lower():
                    time.sleep(20 * (attempt + 1))
                    last_error = error
                    continue
                if error.code >= 500:
                    last_error = error
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"GET {url} failed: {last_error}")

    def get_json(self, url: str, *, allow_404: bool = False):
        raw = self.get(url, api=True, allow_404=allow_404)
        if raw is None:
            return None
        return json.loads(raw)


def readme_repos(readme_path: str) -> list[str]:
    """Return the ordered, de-duplicated GitHub repositories linked in README."""
    with open(readme_path, encoding="utf-8") as handle:
        text = handle.read()

    found: list[str] = []
    seen: set[str] = set()
    for owner, name in REPO_RE.findall(text):
        slug = f"{owner}/{name.rstrip('/')}"
        key = slug.lower()
        if key in {s.lower() for s in SKIP_REPOS} or key in seen:
            continue
        seen.add(key)
        found.append(slug)
    return found


def natural_key(version: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", version)]


def pick_version(versions: list[dict], hints: list[str]) -> dict | None:
    """Choose the version entry the downloaded archive actually contains."""
    usable = [v for v in versions if isinstance(v, dict) and v.get("version")]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    for hint in hints:
        for version in usable:
            if version["version"] in hint:
                return version
    return max(usable, key=lambda v: natural_key(str(v["version"])))


def read_pcm_archive(blob: bytes) -> tuple[dict, bytes | None] | None:
    """Return (metadata, icon_bytes) if the blob is a PCM archive."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return None

    names = set(archive.namelist())
    if "metadata.json" not in names:
        return None
    try:
        metadata = json.loads(archive.read("metadata.json"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    if not metadata.get("identifier") or not isinstance(metadata.get("versions"), list):
        return None

    metadata["_install_size"] = sum(
        info.file_size for info in archive.infolist() if not info.is_dir()
    )

    icon = None
    for candidate in ("resources/icon.png", "resources/icon.PNG"):
        if candidate in names:
            icon = archive.read(candidate)
            break
    return metadata, icon


def build_package(metadata: dict, asset: dict, blob: bytes, tag: str) -> dict | None:
    """Turn an upstream metadata.json plus its archive into a PCM package."""
    version = pick_version(metadata["versions"], [asset["name"], tag])
    if version is None:
        return None
    if not version.get("kicad_version"):
        # PCM refuses to load a version without a compatible KiCad release.
        return None

    entry = {
        "version": str(version["version"]),
        "status": version.get("status", "stable"),
        "kicad_version": str(version["kicad_version"]),
        "download_url": asset["browser_download_url"],
        "download_sha256": hashlib.sha256(blob).hexdigest(),
        "download_size": len(blob),
        "install_size": int(version.get("install_size") or metadata["_install_size"]),
    }
    if version.get("kicad_version_max"):
        entry["kicad_version_max"] = str(version["kicad_version_max"])

    package = {
        key: value
        for key, value in metadata.items()
        if key not in {"versions", "$schema", "_install_size"}
    }
    package["$schema"] = PACKAGE_SCHEMA
    package["versions"] = [entry]
    return package


def collect_from_own_repository(http: Http, slug: str) -> tuple[list[dict], dict[str, bytes]]:
    """Merge a project that already publishes its own PCM packages.json."""
    raw = http.get(
        f"https://raw.githubusercontent.com/{slug}/HEAD/packages.json", allow_404=True
    )
    if raw is None:
        return [], {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return [], {}
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, list) or not packages:
        return [], {}

    installable = [
        package
        for package in packages
        if isinstance(package, dict)
        and package.get("identifier")
        and any(v.get("download_url") for v in package.get("versions", []) if isinstance(v, dict))
    ]
    if not installable:
        return [], {}

    icons: dict[str, bytes] = {}
    resources = http.get(
        f"https://raw.githubusercontent.com/{slug}/HEAD/resources.zip", allow_404=True
    )
    if resources:
        try:
            archive = zipfile.ZipFile(io.BytesIO(resources))
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith("icon.png"):
                    continue
                icons[info.filename.split("/")[0]] = archive.read(info)
        except zipfile.BadZipFile:
            pass

    return installable, icons


def collect_from_releases(
    http: Http, slug: str, log: list[str]
) -> tuple[list[dict], dict[str, bytes]]:
    """Look for PCM archives among the assets of the latest release."""
    release = http.get_json(
        f"https://api.github.com/repos/{slug}/releases/latest", allow_404=True
    )
    if not release:
        return [], {}

    tag = release.get("tag_name", "")
    packages: list[dict] = []
    icons: dict[str, bytes] = {}

    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not name.lower().endswith(".zip"):
            continue
        if asset.get("size", 0) > MAX_ASSET_BYTES:
            log.append(f"{slug}: skipped oversized asset {name} ({asset['size']} bytes)")
            continue

        blob = http.get(asset["browser_download_url"], allow_404=True)
        if blob is None:
            continue
        parsed = read_pcm_archive(blob)
        if parsed is None:
            continue
        metadata, icon = parsed

        if metadata.get("type") not in VALID_TYPES:
            log.append(f"{slug}: {name} has unsupported type {metadata.get('type')!r}")
            continue

        package = build_package(metadata, asset, blob, tag)
        if package is None:
            log.append(f"{slug}: {name} has no usable version entry")
            continue

        packages.append(package)
        if icon:
            icons[package["identifier"]] = icon

    return packages, icons


def write_resources_zip(path: str, icons: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for identifier in sorted(icons):
            info = zipfile.ZipInfo(f"{identifier}/icon.png", date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, icons[identifier])


def file_entry(path: str, url: str, stamp: datetime) -> dict:
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    return {
        "sha256": digest,
        "update_time_utc": stamp.strftime("%Y-%m-%d %H:%M:%S"),
        "update_timestamp": int(stamp.timestamp()),
        "url": url,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", default="README.md")
    parser.add_argument("--out", default="dist")
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "PCM_BASE_URL",
            "https://github.com/joanbono/awesome-kicad/releases/latest/download",
        ),
        help="Public directory the three generated files will be served from.",
    )
    parser.add_argument("--name", default="Awesome KiCad")
    args = parser.parse_args()

    http = Http(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    base_url = args.base_url.rstrip("/")
    os.makedirs(args.out, exist_ok=True)

    repos = readme_repos(args.readme)
    print(f"scanning {len(repos)} GitHub projects from {args.readme}", file=sys.stderr)

    packages: list[dict] = []
    icons: dict[str, bytes] = {}
    seen_identifiers: dict[str, str] = {}
    included: list[tuple[str, list[str]]] = []
    skipped: list[str] = []
    notes: list[str] = []

    for slug in repos:
        try:
            found, found_icons = collect_from_own_repository(http, slug)
            source = "own packages.json"
            if not found:
                found, found_icons = collect_from_releases(http, slug, notes)
                source = "release archive"
        except Exception as error:  # keep one bad upstream from failing the build
            notes.append(f"{slug}: error while scanning ({error})")
            skipped.append(slug)
            continue

        if not found:
            skipped.append(slug)
            continue

        accepted: list[str] = []
        for package in found:
            identifier = package["identifier"]
            if identifier in seen_identifiers:
                notes.append(
                    f"{slug}: identifier {identifier} already provided by "
                    f"{seen_identifiers[identifier]}, keeping the first"
                )
                continue
            seen_identifiers[identifier] = slug
            packages.append(package)
            accepted.append(identifier)
            if identifier in found_icons:
                icons[identifier] = found_icons[identifier]

        if accepted:
            included.append((f"{slug} ({source})", accepted))
            print(f"  + {slug}: {', '.join(accepted)}", file=sys.stderr)
        else:
            skipped.append(slug)

    if not packages:
        print("no installable packages found, refusing to publish", file=sys.stderr)
        return 1

    packages.sort(key=lambda p: p["identifier"])

    packages_path = os.path.join(args.out, "packages.json")
    resources_path = os.path.join(args.out, "resources.zip")
    repository_path = os.path.join(args.out, "repository.json")

    with open(packages_path, "w", encoding="utf-8") as handle:
        json.dump({"packages": packages}, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    write_resources_zip(resources_path, icons)

    stamp = datetime.now(timezone.utc)
    repository = {
        "$schema": REPOSITORY_SCHEMA,
        "name": args.name,
        "maintainer": {
            "name": "joanbono",
            "contact": {"web": "https://github.com/joanbono/awesome-kicad"},
        },
        "packages": file_entry(packages_path, f"{base_url}/packages.json", stamp),
        "resources": file_entry(resources_path, f"{base_url}/resources.zip", stamp),
    }
    with open(repository_path, "w", encoding="utf-8") as handle:
        json.dump(repository, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    report = [
        f"## PCM repository: {len(packages)} package(s) from {len(included)} project(s)",
        "",
        f"Generated {stamp.strftime('%Y-%m-%d %H:%M:%S')} UTC, "
        f"{len(icons)} icon(s) in `resources.zip`.",
        "",
        "### Included",
        "",
    ]
    for label, identifiers in included:
        report.append(f"- **{label}** — {', '.join(identifiers)}")
    report += [
        "",
        f"### Not installable inside KiCad ({len(skipped)})",
        "",
        "No PCM archive published, so nothing for the Plugin and Content Manager "
        "to install:",
        "",
        ", ".join(f"`{slug}`" for slug in skipped) or "_none_",
    ]
    if notes:
        report += ["", "### Notes", ""] + [f"- {note}" for note in notes]
    report.append("")

    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(report))

    print(
        f"wrote {len(packages)} packages, {len(icons)} icons, skipped {len(skipped)} projects",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
