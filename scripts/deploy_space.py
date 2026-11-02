"""Publish the public demo to a Hugging Face Space (Docker SDK, free CPU hardware).

The Space builds the image itself. This uploads exactly what that build needs and nothing
else: Dockerfile.space as `Dockerfile`, space/README.md as the Space card (`README.md`, which
pyproject.toml also reads as the package readme) and the sources. Files in the Space that are
no longer part of the bundle are deleted in the same commit.

    uv run --with huggingface_hub python scripts/deploy_space.py [--space user/name]

Authentication: HF_TOKEN in the environment, or an earlier `hf auth login`. The token is
passed to the Hugging Face client only; this script never prints or stores it.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

ROOT = Path(__file__).resolve().parents[1]
SPACE_NAME = "genomic-etl-pipeline"
# (source in this repository, path in the Space)
FILES = [
    ("Dockerfile.space", "Dockerfile"),
    ("space/README.md", "README.md"),
    ("LICENSE", "LICENSE"),
    ("pyproject.toml", "pyproject.toml"),
    ("uv.lock", "uv.lock"),
    ("alembic.ini", "alembic.ini"),
    ("scripts/generate_data.py", "scripts/generate_data.py"),
    ("space/start.sh", "space/start.sh"),
    ("space/bootstrap.sh", "space/bootstrap.sh"),
    ("space/supervisord.conf", "space/supervisord.conf"),
    ("space/nginx.conf", "space/nginx.conf"),
]
DIRS = ["src", "alembic"]
KEEP_REMOTE = {".gitattributes"}  # created by Hugging Face with the repository


def bundle(target: Path) -> list[str]:
    for source, dest in FILES:
        (target / dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, target / dest)
    for name in DIRS:
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__"))
    paths = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    for path in paths:
        if b"\r\n" in (target / path).read_bytes() and not path.endswith((".fa", ".vcf")):
            raise SystemExit(f"{path} has CRLF line endings; the Linux build would break")
    return paths


def app_url(repo_id: str) -> str:
    """Spaces are served from `<user>-<name>.hf.space`, lower-cased, with _ and . as -."""
    subdomain = repo_id.replace("/", "-").replace("_", "-").replace(".", "-").lower()
    return f"https://{subdomain}.hf.space"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--space", help=f"user/name (default: <your user>/{SPACE_NAME})")
    args = parser.parse_args()

    api = HfApi(token=os.environ.get("HF_TOKEN"))  # None falls back to `hf auth login`
    repo_id = args.space or f"{api.whoami()['name']}/{SPACE_NAME}"
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()

    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        paths = bundle(stage)
        remote = set(api.list_repo_files(repo_id, repo_type="space"))
        operations: list[CommitOperationAdd | CommitOperationDelete] = [
            CommitOperationAdd(path_in_repo=path, path_or_fileobj=str(stage / path))
            for path in paths
        ]
        operations += [
            CommitOperationDelete(path_in_repo=path)
            for path in sorted(remote - set(paths) - KEEP_REMOTE)
        ]
        api.create_commit(
            repo_id,
            repo_type="space",
            operations=operations,
            commit_message=f"Deploy {revision or 'working tree'} of genomic-etl-pipeline",
        )
    print(f"uploaded {len(paths)} files to https://huggingface.co/spaces/{repo_id}")
    print(f"the Space builds now; the app will be at {app_url(repo_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
