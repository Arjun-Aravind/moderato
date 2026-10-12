"""Source and environment fingerprints for new benchmark evidence."""

import hashlib
import subprocess
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def collect_provenance() -> dict[str, Any]:
    # Keep module import dependency-free for the comparison venv bootstrap.
    import moderato

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()

    # The Docker benchmark uses an installed wheel and excludes Git metadata.
    # Unknown provenance is explicit; it must not prevent correctness checks.
    try:
        source_sha = git("rev-parse", "HEAD")
        source_dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
        source_note = "Git checkout metadata available"
    except (FileNotFoundError, subprocess.CalledProcessError):
        source_sha = None
        source_dirty = None
        source_note = "Git metadata unavailable; source revision and dirty state unknown"

    hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "benchmarks").glob("*.py"))
    }
    hashes.update(
        {
            f"moderato/scripts/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((Path(moderato.__file__).parent / "scripts").glob("*.lua"))
        }
    )
    return {
        "source_sha": source_sha,
        "source_dirty": source_dirty,
        "source_note": source_note,
        "sha256": hashes,
        "installed_dependencies": dict(
            sorted((dist.metadata["Name"], dist.version) for dist in distributions())
        ),
    }
