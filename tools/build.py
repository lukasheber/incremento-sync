"""Builds dist/incremento_sync.ankiaddon from the incremento_sync folder (what Anki's "Install from file" takes)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "incremento_sync"
OUT = ROOT / "dist" / "incremento_sync.ankiaddon"


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf8"))
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path, path.relative_to(SOURCE).as_posix())
        archive.write(ROOT / "LICENSE", "LICENSE")
    print(f"{OUT} ({manifest['name']})")


if __name__ == "__main__":
    main()
