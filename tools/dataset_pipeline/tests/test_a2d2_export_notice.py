"""Private Audi derivatives retain their source attribution and licence."""
import pytest

from bcdata.config import load_settings
from bcdata.export_ufld import write_notice


def test_audi_export_bundles_original_notices_byte_for_byte(tmp_path):
    settings = load_settings(overrides={"data_root": str(tmp_path / "data")})
    settings.path("licenses").mkdir(parents=True)
    expected = {"a2d2-LICENSE.txt": b"Complete CC BY-ND licence\r\n",
                "a2d2-NOTICE.txt": b"Audi AG attribution; private local derivatives.\r\n"}
    for name, content in expected.items():
        settings.path("licenses", name).write_bytes(content)
    out = tmp_path / "export"
    out.mkdir()
    write_notice(settings, out, {"a2d2"})
    for name, content in expected.items():
        assert (out / "licenses" / name).read_bytes() == content
    assert "Audi AG attribution" in (out / "NOTICE.txt").read_text()
    assert "CC BY-ND 4.0" in (out / "NOTICE.txt").read_text()


def test_audi_export_refuses_missing_attribution(tmp_path):
    settings = load_settings(overrides={"data_root": str(tmp_path / "data")})
    out = tmp_path / "export"
    out.mkdir()
    with pytest.raises(ValueError, match="Missing Audi export"):
        write_notice(settings, out, {"a2d2"})
    assert not (out / "NOTICE.txt").exists()


def test_other_exports_do_not_require_audi_files(tmp_path):
    settings = load_settings(overrides={"data_root": str(tmp_path / "data")})
    write_notice(settings, tmp_path, {"comma10k"})
    assert not (tmp_path / "licenses").exists()
