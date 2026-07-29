from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests


HISTORICAL_BATCH_URL = "https://hist.databento.com/v0/batch"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class DatabentoApiError(RuntimeError):
    """A redacted Databento failure safe for CloudWatch."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_databento_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    return normalized == "databento.com" or normalized.endswith(".databento.com")


def validate_download_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DatabentoApiError("Databento returned a non-HTTPS download URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise DatabentoApiError("Databento returned an unsafe download URL.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DatabentoApiError("Databento returned an invalid download URL port.") from exc
    if port not in {None, 443}:
        raise DatabentoApiError("Databento returned a download URL on an unexpected port.")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise DatabentoApiError("Databento returned a local download hostname.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise DatabentoApiError("Databento returned a non-public download address.")
    return url


class DatabentoBatchClient:
    """Minimal read-only Batch client.

    It intentionally exposes no submit method, so the cloud ingest worker
    cannot create or purchase a Databento job.
    """

    def __init__(
        self,
        api_key: str,
        *,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._session = session or requests.Session()
        if session is None:
            self._session.trust_env = False
        self._session.headers.update({"User-Agent": "flowdesk-cloud-worker/0.1"})

    @property
    def _timeout(self) -> tuple[int, int]:
        return (self._connect_timeout, self._read_timeout)

    def _get_json(self, method: str, job_id: str) -> Any:
        try:
            response = self._session.get(
                f"{HISTORICAL_BATCH_URL}.{method}",
                params={"job_id": job_id},
                auth=(self._api_key, ""),
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise DatabentoApiError("Databento metadata request failed.", retryable=True) from exc
        try:
            if response.status_code >= 400:
                raise DatabentoApiError(
                    f"Databento metadata request returned HTTP {response.status_code}.",
                    retryable=response.status_code == 429 or response.status_code >= 500,
                )
            return response.json()
        except ValueError as exc:
            raise DatabentoApiError("Databento returned invalid metadata JSON.") from exc
        finally:
            response.close()

    def get_job_details(self, job_id: str) -> dict[str, Any]:
        payload = self._get_json("get_job_details", job_id)
        if not isinstance(payload, dict):
            raise DatabentoApiError("Databento returned invalid job details.")
        return payload

    def list_files(self, job_id: str) -> list[dict[str, Any]]:
        payload = self._get_json("list_files", job_id)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise DatabentoApiError("Databento returned an invalid file list.")
        return payload

    def open_file(self, url: str, *, offset: int = 0) -> requests.Response:
        current_url = validate_download_url(url)
        headers = {"Range": f"bytes={offset}-"} if offset else {}

        for _ in range(6):
            hostname = urlsplit(current_url).hostname or ""
            auth = (self._api_key, "") if _is_databento_hostname(hostname) else None
            try:
                response = self._session.get(
                    current_url,
                    headers=headers,
                    auth=auth,
                    timeout=self._timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise DatabentoApiError("Databento file download failed.", retryable=True) from exc

            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise DatabentoApiError("Databento download redirect had no destination.")
                current_url = validate_download_url(urljoin(current_url, location))
                continue

            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise DatabentoApiError(
                    f"Databento file download returned HTTP {status}.",
                    retryable=status == 429 or status >= 500,
                )
            if offset and response.status_code != 206:
                response.close()
                raise DatabentoApiError("Databento did not honor the requested download resume range.")
            if not offset and response.status_code not in {200, 206}:
                response.close()
                raise DatabentoApiError("Databento returned an unexpected download response.")
            return response

        raise DatabentoApiError("Databento returned too many download redirects.")
