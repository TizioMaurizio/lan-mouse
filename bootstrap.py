"""Double-click start.cmd on Windows, or run ./start.sh on Linux."""

import hashlib
import subprocess
import sys
import venv
from pathlib import Path


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Please install Python 3.11 or newer, then run the launcher again.")
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        print("Preparing LAN Mouse (first run only)…", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    stamp = environment / ".lanmouse-requirements"
    digest = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    if not stamp.exists() or stamp.read_text() != digest:
        print("Installing dependencies…", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "-e", str(root)], check=True)
        stamp.write_text(digest)
    return subprocess.call([str(python), "-m", "lanmouse"], cwd=root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        if sys.platform == "linux":
            print("Run ./setup-linux.sh once, then try ./start.sh again.", file=sys.stderr)
        raise SystemExit(1) from exc
