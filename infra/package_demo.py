"""Build the public demo Lambda's code: `python infra/package_demo.py OUT` (sam build calls it via the Makefile).

Copies the app, the demo script it seeds from and the sample orders, then pip-installs
infra/public-demo-requirements.txt as wheels for the Lambda (Python 3.12, arm64). `--local` installs for
this machine instead, to run the built handler here (docs/aws.md, "Checking the template yourself").
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "infra" / "public-demo-requirements.txt"
TREES = ["app"]
FILES = ["scripts/__init__.py", "scripts/demo.py", "scripts/feed_webhook.py", "samples/orders.csv",
         "samples/past-orders.csv"]
LAMBDA = ["--platform", "manylinux2014_aarch64", "--implementation", "cp", "--python-version", "3.12",
          "--only-binary=:all:"]


def copy_code(out: Path) -> list[Path]:
    written = []
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in TREES:
        shutil.copytree(ROOT / rel, out / rel, ignore=ignore, dirs_exist_ok=True)
        written += [p for p in (out / rel).rglob("*") if p.is_file()]
    for rel in FILES:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, out / rel)
        written.append(out / rel)
    return written


def install_deps(out: Path, local: bool = False) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--no-compile", "--upgrade", "--target", str(out), "-r", str(REQUIREMENTS),
                    *([] if local else LAMBDA)], check=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--local"]
    if len(args) != 1:
        sys.exit("usage: python infra/package_demo.py [--local] OUT_DIR")
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    install_deps(out, local="--local" in sys.argv)
    print(f"{len(copy_code(out))} files of Shopfront code in {out}")
