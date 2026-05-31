"""Build a Windows executable with PyInstaller.

Usage:
    conda run -n baf python scripts/build_exe.py
    conda run -n baf python scripts/build_exe.py --onefile
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Bilibili ADs Flak executable")
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="Build a single exe instead of the default portable folder.",
    )
    args = parser.parse_args()

    mode = "--onefile" if args.onefile else "--onedir"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        mode,
        "--specpath",
        str(ROOT / "build" / "pyinstaller"),
        "--name",
        "BilibiliADsFlak",
        "--icon",
        str(ROOT / "icon.png"),
        "--add-data",
        f"{ROOT / 'icon.png'};.",
        "--add-data",
        f"{ROOT / 'prompts'};prompts",
        "--collect-all",
        "bilibili_api",
        "--hidden-import",
        "bilibili_api.user",
        "--hidden-import",
        "bilibili_api.video",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "scipy",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "torch",
        "--exclude-module",
        "transformers",
        "--exclude-module",
        "sklearn",
        "--exclude-module",
        "IPython",
        "--exclude-module",
        "jedi",
        "--exclude-module",
        "tkinter",
        str(ROOT / "src" / "gui" / "run.py"),
    ]

    subprocess.run(command, cwd=ROOT, check=True)
    if args.onefile:
        print(f"\nBuilt: {ROOT / 'dist' / 'BilibiliADsFlak.exe'}")
    else:
        print(f"\nBuilt: {ROOT / 'dist' / 'BilibiliADsFlak' / 'BilibiliADsFlak.exe'}")


if __name__ == "__main__":
    main()
