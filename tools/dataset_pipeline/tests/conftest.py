import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def settings(tmp_path):
    from bcdata.config import ensure_layout, load_settings
    s = load_settings(overrides={"data_root": str(tmp_path / "ds"), "target_images": 50,
                                 "embedding": {"backend": "cheap"}})
    ensure_layout(s)
    return s


@pytest.fixture()
def registry(settings):
    from bcdata.db import Registry
    r = Registry(settings.db_path)
    yield r
    r.close()


@pytest.fixture()
def licenses(settings):
    from bcdata.licenses import LicenseRegistry
    return LicenseRegistry(settings.licenses_path)
