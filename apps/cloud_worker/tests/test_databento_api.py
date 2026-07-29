from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from apps.cloud_worker.databento_api import (
    DatabentoApiError,
    DatabentoBatchClient,
    validate_download_url,
)


@dataclass
class FakeResponse:
    status_code: int
    payload: object | None = None
    headers: dict[str, str] = field(default_factory=dict)
    closed: bool = False

    def json(self) -> object:
        return self.payload

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **parameters: object) -> FakeResponse:
        self.calls.append({"url": url, **parameters})
        return self.responses.pop(0)


def client(session: FakeSession) -> DatabentoBatchClient:
    return DatabentoBatchClient(
        "db-" + "a" * 32,
        connect_timeout_seconds=10,
        read_timeout_seconds=120,
        session=session,  # type: ignore[arg-type]
    )


def test_metadata_calls_are_read_only_and_authenticated() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"id": "GLBX-20260716-J3TTEVHVW8"}),
            FakeResponse(200, [{"filename": "metadata.json"}]),
        ]
    )
    api = client(session)
    assert not hasattr(api, "submit_job")
    assert api.get_job_details("GLBX-20260716-J3TTEVHVW8")["id"].startswith("GLBX-")
    assert api.list_files("GLBX-20260716-J3TTEVHVW8")[0]["filename"] == "metadata.json"
    assert all(call["auth"][0].startswith("db-") for call in session.calls)
    assert all(call["allow_redirects"] is False for call in session.calls)


def test_download_redirect_does_not_forward_key_to_s3() -> None:
    first = FakeResponse(302, headers={"Location": "https://bucket.s3.eu-central-1.amazonaws.com/object?signature=x"})
    second = FakeResponse(200)
    session = FakeSession([first, second])
    response = client(session).open_file("https://hist.databento.com/v0/batch/download/file")
    assert response is second
    assert session.calls[0]["auth"][0].startswith("db-")
    assert session.calls[1]["auth"] is None
    assert first.closed is True


@pytest.mark.parametrize(
    "url",
    [
        "http://hist.databento.com/file",
        "https://user:password@hist.databento.com/file",
        "https://127.0.0.1/file",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/file",
        "https://example.com:8443/file",
    ],
)
def test_unsafe_download_urls_are_blocked(url: str) -> None:
    with pytest.raises(DatabentoApiError):
        validate_download_url(url)


def test_resume_requires_partial_content_response() -> None:
    response = FakeResponse(200)
    with pytest.raises(DatabentoApiError, match="resume range"):
        client(FakeSession([response])).open_file("https://hist.databento.com/file", offset=10)
    assert response.closed is True
