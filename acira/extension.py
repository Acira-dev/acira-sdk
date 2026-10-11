"""Export the packaged Chrome extension without modifying browser profiles."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from importlib.resources import files
from pathlib import Path

from .local import state_dir


def extension_source():
    packaged = files("acira").joinpath("resources/capture_extension")
    if packaged.joinpath("manifest.json").is_file():
        return packaged
    # Development checkout only; an installed wheel supplies its own resources.
    development = Path(__file__).resolve().parents[1] / "tools/acira-capture-extension"
    if development.joinpath("manifest.json").is_file():
        return development
    raise ValueError("Capture extension assets are missing. Reinstall a verified Acira wheel.")


def extension_files():
    def walk(root, prefix=""):
        for item in sorted(root.iterdir(), key=lambda item: item.name):
            if item.name.startswith(".") or item.name == "__pycache__":
                continue
            relative = prefix + item.name
            if item.is_dir():
                yield from walk(item, relative + "/")
            elif item.is_file():
                yield relative, item.read_bytes()
    return dict(walk(extension_source()))


def install_extension(directory=None, *, replace=False):
    destination = Path(directory).expanduser().absolute() if directory else state_dir() / "capture-extension"
    if destination.is_symlink():
        raise ValueError("The extension folder cannot be a symbolic link")
    assets = extension_files()
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in assets.items()}
    receipt = destination / ".acira-extension.json"
    if destination.exists():
        if not destination.is_dir() or not receipt.is_file() or receipt.is_symlink():
            raise ValueError("Choose an empty new folder; this folder is not an Acira extension installation")
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        if previous.get("schema") != "acira.extension_install.v1":
            raise ValueError("Invalid extension installation receipt")
        actual = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in destination.rglob("*") if p.is_file() and p != receipt and not p.is_symlink()}
        if any(p.is_symlink() for p in destination.rglob("*")):
            raise ValueError("The extension installation contains symbolic links")
        if actual == hashes:
            return destination
        # Never delete unrelated files, including when an update is requested.
        if set(actual) - set(previous.get("files", {})):
            raise ValueError("The extension folder contains additional files; choose a new folder")
        if not replace:
            raise ValueError("The extension differs. Use --replace to update this Acira installation, or choose a new folder")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".acira-extension-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "extension"
        staged.mkdir()
        for name, value in assets.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        (staged / ".acira-extension.json").write_text(json.dumps({"schema": "acira.extension_install.v1", "files": hashes}, indent=2)+"\n")
        if destination.exists():
            backup = Path(temporary) / "previous"
            os.replace(destination, backup)
            try:
                os.replace(staged, destination)
            except BaseException:
                os.replace(backup, destination)
                raise
        else:
            os.replace(staged, destination)
    return destination
