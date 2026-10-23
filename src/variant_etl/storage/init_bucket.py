"""One-shot bucket initialisation, run by the `bucket-init` compose service."""

import time

from botocore.exceptions import BotoCoreError, ClientError

from variant_etl.storage.s3 import get_storage


def main() -> None:
    storage = get_storage()
    for attempt in range(1, 31):
        try:
            storage.ensure_bucket()
            print(f"bucket '{storage.bucket}' ready")
            return
        except (BotoCoreError, ClientError) as exc:
            print(f"waiting for object storage (attempt {attempt}): {exc}")
            time.sleep(2)
    raise SystemExit("object storage did not become available")


if __name__ == "__main__":
    main()
