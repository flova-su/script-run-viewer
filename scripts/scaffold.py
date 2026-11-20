#!/usr/bin/env python3
"""Create a self-contained project with a runnable browser viewer."""

import argparse
import shutil
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path)
args = parser.parse_args()
source = Path(__file__).resolve().parents[1] / "assets" / "viewer"
try:
    shutil.copytree(source, args.directory, ignore=shutil.ignore_patterns(".script-viewer", "__pycache__", ".DS_Store"))
except FileExistsError:
    parser.error("The destination already exists. Pick a new directory.")
print(args.directory.resolve())
