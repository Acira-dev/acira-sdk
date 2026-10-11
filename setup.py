"""Ship the canonical capture extension with wheels and source distributions."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithCapture(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / "tools" / "acira-capture-extension"
        destination = Path(self.build_lib) / "acira" / "resources" / "capture_extension"
        if not (source / "manifest.json").is_file():
            raise RuntimeError("The capture extension is missing from the source distribution")
        shutil.copytree(source, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


setup(cmdclass={"build_py": BuildWithCapture})
