"""Fetch a reference FASTA from object storage into a worker-local cache and open it.

pyfaidx needs a seekable file plus its .fai index, so the object is downloaded once per
worker and reused by every later task. Upload keys are write-once (each contains a random
id), so the key alone identifies the content and no revalidation is needed.
"""

import hashlib
import os
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from variant_etl.config import get_settings
from variant_etl.pipeline.reference import FastaReference
from variant_etl.pipeline.textio import maybe_gunzip
from variant_etl.storage.s3 import ObjectStorage


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()
    return Path(get_settings().reference_cache_dir) / f"{digest}.fa"


def ensure_local_reference(storage: ObjectStorage, key: str) -> Path:
    """Download (and decompress, if gzipped) the reference once; build its .fai atomically.

    The work happens in a private temp directory and is moved into place with os.replace,
    FASTA first and the .fai last. The .fai is therefore the "complete" marker: a concurrent
    reader never sees a half-written FASTA or index.
    """
    final = _cache_path(key)
    final_fai = Path(str(final) + ".fai")
    if final_fai.exists():
        return final

    final.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=final.parent, prefix="dl-"))
    try:
        local = work / "ref.fa"
        with local.open("wb") as out:
            for block in maybe_gunzip(storage.iter_blocks(key)):
                out.write(block)
        FastaReference(local).close()  # side effect: pyfaidx writes ref.fa.fai
        os.replace(local, final)
        os.replace(Path(str(local) + ".fai"), final_fai)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return final


@lru_cache(maxsize=2)
def open_reference(storage: ObjectStorage, key: str) -> FastaReference:
    return FastaReference(ensure_local_reference(storage, key))
