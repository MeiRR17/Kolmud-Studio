#!/usr/bin/env python3
"""Opt-in local HyperFrames renderer for ORIGINAL licensed studio media.

Does not download, install or invoke npx. Requires an operator-installed
HyperFrames binary and an existing approved local project outside Git.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def prepare(project: Path, binary: str = "hyperframes") -> dict:
    if project.is_symlink() or not project.is_dir():
        raise ValueError("Project must be an existing non-symlink directory")
    folder = project.resolve()
    if REPO in folder.parents or folder == REPO:
        raise ValueError("Render only from a separate local project, never within Git")
    if any(part.is_symlink() for part in (folder, folder.parent)):
        raise ValueError("Symlinked render paths not permitted")
    if not (folder / "package.json").is_file():
        raise ValueError("Only an existing approved HyperFrames project is accepted")
    exe = shutil.which(binary)
    return {
        "command": [binary, "render"], "cwd": str(folder),
        "binary_available": bool(exe), "project": str(folder),
        "rights_confirmed": False, "auto_publish": False,
        "cloud_provider_enabled": False,
    }


def render(project: Path, runner=subprocess.run, binary: str = "hyperframes"):
    plan = prepare(project, binary)
    if not plan["binary_available"]:
        raise RuntimeError("HyperFrames binary not installed; no automatic npx install")
    # Remove tokens, credentials and provider keys: renderer should process
    # only locally prepared assets. OS-level network blocking is still needed.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
           if k in os.environ}
    env["CI"] = "true"
    env["NODE_ENV"] = "production"
    result = runner(plan["command"], cwd=plan["cwd"], env=env,
                    capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError("Renderer failed (exit " + str(result.returncode) + ")")
    return {"status": "RENDER_COMMAND_COMPLETED",
            "media_rights_attested": False,
            "project": plan["project"], "renderer": binary,
            "runtime_verified_output": False,
            "notice": "Inspect output MP4, assets, subtitles and rights manually before publishing"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Explicit local render")
    options = parser.parse_args()
    try:
        result = render(options.project) if options.run else prepare(options.project)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, "Local render refused: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
