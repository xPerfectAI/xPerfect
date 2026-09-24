"""Exercise the delivered response iterator against files changed between chunks."""

import asyncio
import errno
import os
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
import pytest

from workers_projects_runtime.workspace_file_api import install_workspace_file_routes
from workers_projects_runtime.workspace_file_exports import CHUNK_BYTES
from workers_projects_runtime.workspace_files import (
    FileAdmissionError,
    _directory,
    _snapshot_fd,
)


@pytest.fixture
def download(tmp_path):
    path = tmp_path / "data.bin"

    class Source:
        descriptor = None

        def open_download(
            self, worker_id, tenant_id, owner_id, file_id, *, revision=None
        ):
            assert (worker_id, tenant_id, owner_id, file_id) == (
                "worker",
                "tenant",
                "owner",
                "file",
            )
            with _directory(tmp_path) as parent:
                descriptor, current, size = _snapshot_fd(parent, path.name)
            self.descriptor = descriptor
            if revision is not None and revision != current:
                os.close(descriptor)
                raise FileAdmissionError("Version changed", 409)
            return descriptor, path.name, size

    source = Source()
    app = FastAPI()
    install_workspace_file_routes(
        app,
        source,
        lambda _: SimpleNamespace(
            auth_mode="session",
            tenant_id="tenant",
            owner_id="owner",
            role="viewer",
            enterprise=True,
        ),
        lambda worker_id, request: None,
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "name", "") == "download"
    )

    def response(range_header=None):
        headers = [(b"range", range_header.encode())] if range_header else []
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path_params": {"worker_id": "worker"},
                "headers": headers,
            }
        )
        return endpoint("worker", "file", request)

    return path, source, response


def assert_closed(source):
    with pytest.raises(OSError) as error:
        os.fstat(source.descriptor)
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize(
    "size,range_header,expected_status,start,end",
    [
        (0, None, 200, 0, 0),
        (CHUNK_BYTES * 2 + 17, None, 200, 0, CHUNK_BYTES * 2 + 17),
        (CHUNK_BYTES * 2 + 17, "bytes=3-262150", 206, 3, 262151),
        (10, "bytes=-3", 206, 7, 10),
    ],
)
def test_clean_empty_and_range_downloads_preserve_bytes_and_close(
    download, size, range_header, expected_status, start, end
):
    path, source, make_response = download
    payload = (bytes(range(256)) * (size // 256 + 1))[:size]
    path.write_bytes(payload)
    response = make_response(range_header)

    async def consume():
        chunks = [chunk async for chunk in response.body_iterator]
        await response.background()
        return chunks

    chunks = asyncio.run(consume())
    assert response.status_code == expected_status
    assert int(response.headers["content-length"]) == end - start
    assert b"".join(chunks) == payload[start:end]
    assert max(map(len, chunks), default=0) <= CHUNK_BYTES
    assert_closed(source)


@pytest.mark.parametrize("mutation", ["truncate", "append", "rewrite"])
def test_mutation_between_chunks_aborts_and_closes(download, mutation):
    path, source, make_response = download
    path.write_bytes(b"a" * (CHUNK_BYTES * 3))
    response = make_response()

    async def consume():
        first = await anext(response.body_iterator)
        assert first == b"a" * CHUNK_BYTES
        original = path.stat()
        if mutation == "truncate":
            with path.open("r+b") as output:
                output.truncate(CHUNK_BYTES)
        elif mutation == "append":
            with path.open("ab") as output:
                output.write(b"appended")
        else:
            with path.open("r+b") as output:
                output.seek(CHUNK_BYTES + 20)
                output.write(b"replacement")
            # Preserving size and mtime must still be detected through ctime.
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with pytest.raises(
            FileAdmissionError, match="changed during download"
        ) as error:
            await anext(response.body_iterator)
        assert error.value.status_code == 409
        await response.background()

    asyncio.run(consume())
    assert_closed(source)


def test_append_after_last_chunk_cannot_complete_silently(download):
    path, source, make_response = download
    path.write_bytes(b"one chunk")
    response = make_response()

    async def consume():
        assert await anext(response.body_iterator) == b"one chunk"
        with path.open("ab") as output:
            output.write(b"new")
        with pytest.raises(FileAdmissionError, match="changed during download"):
            await anext(response.body_iterator)
        await response.background()

    asyncio.run(consume())
    assert_closed(source)


def test_changed_before_first_chunk_and_invalid_range_close(download):
    path, source, make_response = download
    path.write_bytes(b"initial")
    response = make_response()
    path.write_bytes(b"changed")

    async def consume():
        with pytest.raises(FileAdmissionError, match="changed during download"):
            await anext(response.body_iterator)
        await response.background()

    asyncio.run(consume())
    assert_closed(source)
    with pytest.raises(HTTPException) as error:
        make_response("bytes=100-")
    assert error.value.status_code == 416
    assert_closed(source)


def test_background_cleanup_closes_a_response_never_consumed(download):
    path, source, make_response = download
    path.write_bytes(b"initial")
    response = make_response()
    asyncio.run(response.background())
    assert_closed(source)
