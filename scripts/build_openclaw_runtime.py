#!/usr/bin/env python3
"""Build an immutable JS-only OpenClaw plugin; Python/data stay in the host."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile


def build_runtime(root: Path) -> Path:
    root = root.resolve()
    source_package = json.loads((root / "package.json").read_text())
    # Never copy install hooks: this package must not create a .venv on load/install.
    package = {key: source_package[key] for key in (
        "name", "version", "description", "main", "license", "openclaw"
    )}
    files = {"package.json": (json.dumps(package, indent=2) + "\n").encode()}
    for name in ("openclaw.plugin.json", "src/index.js", "skill/SKILL.md", "LICENSE", "README.md"):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Runtime source must stay inside the project: {name}")
        files[name] = path.read_bytes()
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + content + b"\0")
    parent = root / "dist" / "openclaw-runtime"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / digest.hexdigest()[:16]
    if target.exists():
        actual = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        if any(p.is_symlink() for p in target.rglob("*")) or actual != files:
            raise ValueError("Existing runtime was modified; refusing to reuse it")
        return target
    with tempfile.TemporaryDirectory(prefix=".build-", dir=parent) as temp:
        stage = Path(temp) / "package"
        for name, content in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        stage.rename(target)
    return target


if __name__ == "__main__":
    print(build_runtime(Path(__file__).resolve().parent.parent))
