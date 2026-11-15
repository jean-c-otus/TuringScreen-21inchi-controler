#!/usr/bin/env python3
"""Produce a directory bundle and tar.gz; exclude all personal workspace data."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise SystemExit("Build this package on Arch Linux x86_64")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(ROOT / "build/pyinstaller-dist"),
                    str(ROOT / "packaging/turing-studio.spec")], cwd=ROOT, check=True)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    package = dist / "installer"
    if package.exists():
        shutil.rmtree(package)
    package.mkdir()
    shutil.copytree(ROOT / "build/pyinstaller-dist/turing-studio", package / "app")
    shutil.copy2(ROOT / "packaging/install.sh", package / "install.sh")
    (package / "install.sh").chmod(0o755)
    shutil.copy2(ROOT / "packaging/99-turing-studio.rules", package / "99-turing-studio.rules")
    shutil.copy2(ROOT / "DESKTOP.md", package / "DESKTOP.md")
    # Include the exact application source used by this build for review and
    # upstream attribution, without config/, source/, example/, or browser data.
    source = package / "source-code"
    source.mkdir()
    for pattern in ("*.py", "requirements*.txt", "README.md", "DESKTOP.md", "CHANGELOG*.md"):
        for file in ROOT.glob(pattern):
            shutil.copy2(file, source / file.name)
    for name in ("static", "tests", "packaging"):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
    distributions = ("Flask", "Werkzeug", "Jinja2", "MarkupSafe", "blinker", "click",
                     "itsdangerous", "Pillow", "psutil", "pyusb", "pyserial", "dbus-next", "PyInstaller")
    versions = {}
    for name in distributions:
        distribution = importlib.metadata.distribution(name)
        versions[name] = distribution.version
        for file in distribution.files or []:
            if "license" in str(file).lower() or Path(file).name.upper().startswith("COPYING"):
                candidate = Path(distribution.locate_file(file))
                if candidate.is_file():
                    target = package / "third-party-licenses" / name / Path(file).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, target)
    libusb_version = subprocess.run(["pkg-config", "--modversion", "libusb-1.0"],
                                    check=True, capture_output=True, text=True).stdout.strip()
    libusb_license = Path("/usr/share/licenses/libusb")
    if libusb_license.is_dir():
        shutil.copytree(libusb_license, package / "third-party-licenses/libusb")
    else:
        license_file = Path("/usr/share/licenses/spdx/LGPL-2.1-or-later.txt")
        if not license_file.is_file():
            raise RuntimeError("libusb license text is required for packaging")
        license_target = package / "third-party-licenses/libusb"
        license_target.mkdir(parents=True)
        shutil.copy2(license_file, license_target / "COPYING")
    metadata = {"built_at": datetime.now(timezone.utc).isoformat(),
                "platform": platform.platform(), "python": sys.version,
                "dependencies": versions, "bundled_libusb": libusb_version,
                "external_dependencies": ["Chrome/Chromium", "FFmpeg"],
                "entrypoint": "app/turing-studio", "personal_data_included": False}
    (package / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
    # Keep a checksum manifest for every shipped file.
    lines = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path.relative_to(package))
             for path in sorted(package.rglob("*")) if path.is_file()]
    (package / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    archive = dist / (package.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as output:
        output.add(package, arcname=package.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(digest + "  " + archive.name + "\n")
    # Retire only known generated artifacts from the previous package layout.
    # Intermediate executable bundles now live under build/, outside dist/.
    for name in ("turing-studio", "turing-studio-arch-x86_64"):
        old = dist / name
        if old.is_dir() and (old / "app/turing-studio" if name.endswith("x86_64") else old / "turing-studio").is_file():
            shutil.rmtree(old)
    for name in ("turing-studio-arch-x86_64.tar.gz", "turing-studio-arch-x86_64.tar.gz.sha256"):
        (dist / name).unlink(missing_ok=True)
    print(f"Package: {archive}\nSize: {archive.stat().st_size / 1024 / 1024:.1f} MiB\nSHA256: {digest}")


if __name__ == "__main__":
    main()
