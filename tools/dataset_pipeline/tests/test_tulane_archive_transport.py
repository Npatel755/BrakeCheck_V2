from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import re
import zipfile

import pytest
import requests

from bcdata import import_tulane
from bcdata.http_range_zip import HTTPRangeReader, SharedCooldown, request, retry_seconds
from bcdata.import_tulane import SyntheticSourceArchive, download_url
from tests.test_import_tulane import fixtures, jpg, source_records


class Response:
    def __init__(self, raw=b"", *, status=206, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = io.BytesIO(raw)
        self.url = "https://storage.googleapis.com/official/archive.zip?secret=not-for-receipts"
        self.closed = False

    def close(self):
        self.closed = True


class RangeServer:
    def __init__(self, data):
        self.data, self.requests = data, []
        self.closed = False

    def get(self, url, *, headers, **kwargs):
        self.requests.append((url, headers["Range"]))
        match = re.fullmatch(r"bytes=([0-9]+)-([0-9]+)", headers["Range"])
        start, end = map(int, match.groups())
        assert 0 <= start <= end < len(self.data)
        return Response(self.data[start:end + 1], headers={"Content-Range": f"bytes {start}-{end}/{len(self.data)}"})

    def close(self):
        self.closed = True


def archive_bytes(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return out.getvalue()


def test_zip_range_reader_resolves_api_only_once_and_limits_cache():
    raw = archive_bytes({"source/image.jpg": jpg(), "target/never-open.jpg": b"unwanted domain"})
    session = RangeServer(raw)
    reader = HTTPRangeReader("https://www.kaggle.com/pinned-v2", session=session, block_size=64, max_blocks=2)
    with zipfile.ZipFile(reader) as archive:
        assert archive.read("source/image.jpg") == jpg()
    assert sum(url.startswith("https://www.kaggle.com") for url, _ in session.requests) == 1
    assert all("storage.googleapis.com" in url for url, _ in session.requests[1:])
    assert len(reader._cache) <= 2
    reader.seek(100)
    assert reader.read(20) == raw[100:120]
    reader.seek(-4, io.SEEK_END)
    assert reader.read() == raw[-4:]
    reader.seek(2, io.SEEK_END)
    assert reader.read(10) == b""
    with pytest.raises(ValueError):
        reader.seek(-1)
    reader.close()
    assert not session.closed  # Caller retains its injected session.
    with pytest.raises(ValueError, match="closed"):
        reader.read(1)


@pytest.mark.parametrize("status,header,raw", [(200, "", b"P"), (206, "bytes 0-1/50", b"P"),
    (206, "bytes 0-0/50", b"PK"), (206, "bytes 0-0/50", b"X")])
def test_range_probe_refuses_ignored_mismatched_or_invalid_ranges(status, header, raw):
    response = Response(raw, status=status, headers={"Content-Range": header})
    class Server:
        def get(self, *args, **kwargs):
            return response
    with pytest.raises(ValueError):
        HTTPRangeReader("https://official/pinned", session=Server())
    assert response.closed


def test_subsequent_range_response_must_match_offsets_and_length():
    session = RangeServer(b"PK" + b"z" * 100)
    reader = HTTPRangeReader("https://official/pinned", session=session, block_size=16)
    def bad(*args, **kwargs):
        return Response(b"short", headers={"Content-Range": "bytes 0-15/102"})
    session.get = bad
    with pytest.raises(ValueError, match="length"):
        reader.read(10)
    reader.close()


def test_archive_etag_change_cannot_mix_two_versions():
    class Server(RangeServer):
        def get(self, url, *, headers, **kwargs):
            response = super().get(url, headers=headers, **kwargs)
            response.headers["ETag"] = '"original"' if len(self.requests) == 1 else '"changed"'
            if len(self.requests) > 1:
                assert headers["If-Match"] == '"original"'
            return response
    reader = HTTPRangeReader("https://official/pinned", session=Server(b"PK" + b"z" * 100), block_size=16)
    with pytest.raises(ValueError, match="ETag changed"):
        reader.read(10)
    reader.close()


def test_retry_after_http_date_and_shared_floor_are_respected():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert retry_seconds("300", limited=True) == 300
    assert retry_seconds(format_datetime(now + timedelta(seconds=180)), now=now, limited=True) == 180
    assert retry_seconds("10", limited=True) == 60
    assert retry_seconds(None, attempt=2) == 8
    ticks, sleeps = [0.0], []
    def sleep(delay):
        sleeps.append(delay)
        ticks[0] += delay
    cooldown = SharedCooldown(clock=lambda: ticks[0], sleep=sleep)
    responses = [Response(status=429, headers={"Retry-After": "300"}), Response(b"P")]
    class Server:
        def get(self, *args, **kwargs):
            return responses.pop(0)
    assert request(Server(), "https://official/pinned", cooldown=cooldown).status_code == 206
    assert sleeps == [300]
    cooldown.defer(100)
    cooldown.defer(5)
    cooldown.wait()
    assert sleeps[-1] == 100  # A later worker cannot shorten the shared deadline.


def test_signed_transport_url_is_not_exposed_in_connection_errors():
    class Server:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError("secret=private-signature")
    with pytest.raises(RuntimeError) as failure:
        request(Server(), "https://storage.googleapis.com/x?secret=private-signature", attempts=1)
    assert "private-signature" not in str(failure.value)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_access_errors_and_missing_objects_are_not_retried_or_bypassed(status):
    calls = []
    class Server:
        def get(self, *args, **kwargs):
            calls.append(1)
            return Response(status=status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        request(Server(), "https://official/pinned")
    assert len(calls) == 1


def test_archive_fetch_reads_only_exact_pinned_synthetic_members(tmp_path, monkeypatch):
    expected = fixtures(tmp_path)
    records = source_records(tmp_path / "upstream", expected=expected)
    monkeypatch.setattr(import_tulane, "source_records", lambda path: records)
    members = {"CARLANE/TuLane/data/" + row["raw_file"]: jpg() for row in records}
    members["CARLANE/TuLane/data/train/target/Town04/straight/0000_image.jpg"] = b"not allowed"
    reader_calls = []
    def open_archive(url):
        reader_calls.append(url)
        return io.BytesIO(archive_bytes(members))
    fetcher = SyntheticSourceArchive(tmp_path, reader_factory=open_archive)
    assert fetcher(download_url(records[0]["raw_file"])) == jpg()
    assert fetcher(download_url(records[-1]["raw_file"])) == jpg()
    assert len(reader_calls) == 1 and reader_calls[0].endswith("?datasetVersionNumber=2")
    with pytest.raises(ValueError, match="source"):
        fetcher(download_url("train/target/Town04/straight/0000_image.jpg"))
    with pytest.raises(ValueError, match="absent"):
        fetcher(download_url("train/source/Town04/straight/9999_image.jpg"))
    with pytest.raises(ValueError, match="pinned version"):
        fetcher(download_url(records[0]["raw_file"]).replace("Number=2", "Number=3"))
    fetcher.close()


def test_archive_missing_expected_member_fails_once_without_more_api_requests(tmp_path, monkeypatch):
    expected = fixtures(tmp_path)
    records = source_records(tmp_path / "upstream", expected=expected)
    monkeypatch.setattr(import_tulane, "source_records", lambda path: records)
    calls = []
    def open_archive(url):
        calls.append(url)
        return io.BytesIO(archive_bytes({"other.txt": b"wrong version"}))
    fetcher = SyntheticSourceArchive(tmp_path, reader_factory=open_archive)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="could not be opened"):
            fetcher(download_url(records[0]["raw_file"]))
    assert len(calls) == 1


def test_corrupted_member_crc_never_becomes_a_valid_source_image(tmp_path, monkeypatch):
    expected = fixtures(tmp_path)
    records = source_records(tmp_path / "upstream", expected=expected)
    monkeypatch.setattr(import_tulane, "source_records", lambda path: records)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_STORED) as archive:
        for row in records:
            archive.writestr("CARLANE/TuLane/data/" + row["raw_file"], jpg())
    raw = bytearray(data.getvalue())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo("CARLANE/TuLane/data/" + records[0]["raw_file"])
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    raw[offset + 100] ^= 1
    fetcher = SyntheticSourceArchive(tmp_path, reader_factory=lambda url: io.BytesIO(raw))
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        fetcher(download_url(records[0]["raw_file"]))
    assert not (tmp_path / "images").exists()
    fetcher.close()
