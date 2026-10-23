"""Small HTTP client for the service, shared by demo.py and benchmark.py."""

import time
from pathlib import Path
from typing import Any

import httpx


class JobFailed(Exception):
    pass


class ApiClient:
    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self.http = httpx.Client(base_url=base_url, timeout=120)

    def wait_ready(self, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.http.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit("API did not become healthy; is the stack up? (`make up`)")
            time.sleep(1)

    def upload(self, path: Path) -> str:
        """Presigned-URL upload: the bytes go straight to object storage, not via the API."""
        response = self.http.post("/api/v1/uploads", json={"filename": path.name})
        response.raise_for_status()
        body = response.json()
        # Read fully: S3 PUT needs a Content-Length, which a streamed body would not send.
        put = httpx.put(body["upload_url"], content=path.read_bytes(), timeout=600)
        put.raise_for_status()
        return str(body["key"])

    def create_job(self, vcf_key: str, reference_key: str) -> dict[str, Any]:
        response = self.http.post(
            "/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": reference_key}
        )
        response.raise_for_status()
        return dict(response.json())

    def job(self, job_id: str) -> dict[str, Any]:
        response = self.http.get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        return dict(response.json())

    def wait_job(
        self, job_id: str, timeout: float = 1800, on_progress: Any = None
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.job(job_id)
            if on_progress:
                on_progress(job)
            if job["status"] == "completed":
                return job
            if job["status"] == "failed":
                raise JobFailed(job["error"])
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} still {job['status']} after {timeout}s")
            time.sleep(0.5)

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.http.get(path, params=params)
        response.raise_for_status()
        return dict(response.json())
