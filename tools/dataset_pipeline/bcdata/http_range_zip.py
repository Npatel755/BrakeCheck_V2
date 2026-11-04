"""Read selected members of one official ZIP without downloading other domains.

The public archive redirect is resolved once, then its normal HTTPS byte-range
support is used. Signed storage URLs remain in memory and never enter receipts.
"""
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import io
import math
import re
import threading
import time

import requests


class SharedCooldown:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self._clock, self._sleep = clock, sleep
        self._until = 0.0
        self._lock = threading.Lock()

    def wait(self):
        while True:
            with self._lock:
                delay = self._until - self._clock()
            if delay <= 0:
                return
            self._sleep(delay)

    def defer(self, seconds):
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)


COOLDOWN = SharedCooldown()


def retry_seconds(value, *, attempt=0, limited=False, now=None):
    """Honor both legal Retry-After forms, without capping a server's request."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = 0
    floor = 60 if limited else min(60, 2 ** (attempt + 1))
    return max(floor, seconds) if math.isfinite(seconds) else floor


def request(session, url, *, headers=None, attempts=8, cooldown=COOLDOWN):
    """Stream headers first; callers enforce body limits and close responses."""
    for attempt in range(attempts):
        cooldown.wait()
        try:
            response = session.get(url, headers=headers, stream=True, timeout=(20, 90))
        except requests.RequestException as error:
            if attempt + 1 == attempts:
                # requests' error strings can contain a signed redirect URL.
                raise RuntimeError(f"Archive connection failed ({type(error).__name__})") from None
            cooldown.defer(retry_seconds(None, attempt=attempt))
            continue
        status = response.status_code
        if status in {429, 500, 502, 503, 504}:
            delay = retry_seconds(response.headers.get("Retry-After"), attempt=attempt, limited=status == 429)
            response.close()
            cooldown.defer(delay)
            if attempt + 1 < attempts:
                continue
        if not 200 <= status < 300:
            response.close()
            raise RuntimeError(f"Archive download returned HTTP {status}")
        return response
    raise RuntimeError("Archive request attempts exhausted")


class HTTPRangeReader(io.RawIOBase):
    """Seekable, bounded-cache reader for ZipFile; require exact range replies."""
    def __init__(self, url, *, session=None, block_size=4 * 1024 * 1024, max_blocks=4):
        super().__init__()
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._block_size, self._max_blocks = block_size, max_blocks
        self._cache = OrderedDict()
        self._position = 0
        self.bytes_downloaded = 0
        self.range_requests = 0
        response = request(self._session, url, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"})
        try:
            match = re.fullmatch(r"bytes 0-0/([0-9]+)", response.headers.get("Content-Range", ""))
            if response.status_code != 206 or not match:
                raise ValueError("Official archive does not support exact HTTP byte ranges")
            self._size = int(match[1])
            if self._size <= 0:
                raise ValueError("Official archive is empty")
            # Resolve the official redirect once, avoiding per-JPEG Kaggle API calls.
            self._url = response.url
            self._etag = response.headers.get("ETag")
            if response.raw.read(2) != b"P":
                raise ValueError("Official range response did not contain exactly one ZIP byte")
        finally:
            response.close()

    @property
    def size(self):
        return self._size

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._position + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError("Invalid seek origin")
        if target < 0:
            raise ValueError("Negative archive seek")
        self._position = target
        return target

    def _block(self, index):
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        start = index * self._block_size
        end = min(self._size, start + self._block_size) - 1
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        if self._etag:
            headers["If-Match"] = self._etag
        response = request(self._session, self._url, headers=headers)
        try:
            if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{self._size}":
                raise ValueError("Archive range response differs from requested byte offsets")
            if self._etag and response.headers.get("ETag") != self._etag:
                raise ValueError("Archive ETag changed while reading the pinned dataset")
            raw = response.raw.read(end - start + 2)
            if len(raw) != end - start + 1:
                raise ValueError("Archive range response has an unexpected length")
        finally:
            response.close()
        self.bytes_downloaded += len(raw)
        self.range_requests += 1
        self._cache[index] = raw
        while len(self._cache) > self._max_blocks:
            self._cache.popitem(last=False)
        return raw

    def read(self, size=-1):
        if self.closed:
            raise ValueError("Reading closed archive")
        remaining = max(0, self._size - self._position)
        count = remaining if size is None or size < 0 else min(size, remaining)
        chunks = []
        while count:
            index, offset = divmod(self._position, self._block_size)
            raw = self._block(index)
            amount = min(count, len(raw) - offset)
            chunks.append(raw[offset:offset + amount])
            self._position += amount
            count -= amount
        return b"".join(chunks)

    def close(self):
        self._cache.clear()
        if self._owns_session:
            self._session.close()
        super().close()
