"""Assemble locally built SDK distributions and the unpacked-extension archive.

Does not publish or claim platform/pilot verification. Run after building and
verifying the wheel. Outputs checksums so reviewers test the exact same artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import zipfile
from pathlib import Path


def assemble(destination: Path, source: Path | None = None):
    source = source or Path(__file__).resolve().parents[1]
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    wheel = destination / f"acira-{version}-py3-none-any.whl"
    sdist = destination / f"acira-{version}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        raise ValueError("Build the wheel and source distribution before assembling a release")
    extension = source / "tools/acira-capture-extension"
    extension_version = json.loads((extension / "manifest.json").read_text())["version"]
    archive = destination / f"acira-capture-{extension_version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(extension.rglob("*")):
            if path.is_symlink():
                raise ValueError("Extension releases cannot include symbolic links")
            relative = path.relative_to(extension)
            named = {"manifest.json", "background.js", "content.js", "capture.css", "selection.html", "selection.js", "connection.js", "connection.css", "connection.html", "README.md"}
            if path.is_file() and (relative.as_posix() in named or relative.parts[0] == "assets" and path.suffix in {".svg", ".png", ".jpg"}):
                output.write(path, relative)
    artifacts = [wheel, sdist, archive]
    checksums = []
    for path in artifacts:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024*1024), b""):
                digest.update(block)
        checksums.append({"file": path.name, "sha256": digest.hexdigest(), "bytes": path.stat().st_size})
    (destination / "SHA256SUMS").write_text("".join(f"{item['sha256']}  {item['file']}\n" for item in checksums))
    manifest = {"sdk_version": version, "extension_version": extension_version,
        "publication": "not performed by this tool", "verification": "See docs/implementation-status.md and the matching CI run",
        "artifacts": checksums}
    (destination / "release-manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    assemble(parser.parse_args().directory.resolve())
