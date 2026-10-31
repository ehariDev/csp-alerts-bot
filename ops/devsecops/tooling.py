"""Install a checksum-pinned Gitleaks executable into the isolated work directory."""
from __future__ import annotations

import hashlib
import io
import os
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "8.30.0"
ASSETS = {
    "Windows": ("windows_x64.zip", "54fe94f644b832dd08e8c3a5915efb3bfa862386d59fb27ca0792cb687a83573"),
    "Linux": ("linux_x64.tar.gz", "79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e"),
}


def install() -> Path:
    system = platform.system()
    if system not in ASSETS or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("Only x64 Windows and Linux are supported")
    suffix, checksum = ASSETS[system]
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_{suffix}"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed public HTTPS URL
        data = response.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError("Gitleaks download failed integrity verification")
    name = "gitleaks.exe" if system == "Windows" else "gitleaks"
    # Read the exact member; never extract arbitrary archive paths.
    if system == "Windows":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            executable = archive.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = archive.extractfile(name)
            if member is None:
                raise ValueError("Gitleaks executable missing from archive")
            executable = member.read()
    output = Path(__file__).parent / "_work/bin" / name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(executable)
    os.chmod(output, 0o700)
    return output.resolve()


if __name__ == "__main__":
    print(install())
