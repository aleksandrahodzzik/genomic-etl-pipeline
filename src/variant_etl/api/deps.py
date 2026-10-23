from collections.abc import Callable

from variant_etl.storage.s3 import ObjectStorage, get_storage


def storage_dependency() -> ObjectStorage:
    return get_storage()


def enqueue_split(job_id: str) -> None:
    # Imported here so that building the API object does not import the whole pipeline.
    from variant_etl.pipeline.tasks import split_job

    split_job.apply_async(args=[job_id])


def enqueue_dependency() -> Callable[[str], None]:
    """Overridable in tests so the API can be exercised without a broker."""
    return enqueue_split
