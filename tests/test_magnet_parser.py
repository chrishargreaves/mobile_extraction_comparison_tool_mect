"""Tests for magnet_parser module."""

import io
import gzip
import os
import tarfile
import tempfile
import zipfile

import pytest

from magnet_parser import MagnetQuickImageParser
from android_backup_parser import AndroidBackupFile


def _make_tar_bytes(members, mode='w:'):
    """Create a tar archive in memory.

    Args:
        members: list of (name, content_bytes_or_None_for_dir) tuples
        mode: tar write mode ('w:' for raw, 'w:gz' for gzipped)

    Returns:
        bytes of the tar archive
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            if content is None:
                # Directory
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_magnet_zip(tmpdir, adb_members=None, sdcard_members=None, live_data=None):
    """Create a synthetic Magnet Quick Image ZIP.

    Args:
        tmpdir: directory to write the ZIP in
        adb_members: list of (name, content_or_None) for adb-data.tar
        sdcard_members: list of (name, content_or_None) for sdcard.tar.gz (or None to skip)
        live_data: list of (name, content_bytes) for Live Data/ entries (or None)

    Returns:
        path to the created ZIP file
    """
    if adb_members is None:
        adb_members = [
            ("apps/com.example/", None),
            ("apps/com.example/db/", None),
            ("apps/com.example/db/data.db", b"database content"),
            ("apps/com.example/_manifest", b"com.example\n1\n1\n28\n"),
            ("shared/0/", None),
            ("shared/0/DCIM/", None),
            ("shared/0/DCIM/photo.jpg", b"jpeg data"),
        ]

    adb_tar_data = _make_tar_bytes(adb_members)

    zip_path = os.path.join(tmpdir, "Quick Image.zip")
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr("adb-data.tar", adb_tar_data)

        if sdcard_members is not None:
            sdcard_tar_data = _make_tar_bytes(sdcard_members, mode='w:gz')
            zf.writestr("sdcard.tar.gz", sdcard_tar_data)

        if live_data:
            for name, content in live_data:
                zf.writestr(f"Live Data/{name}", content)

    return zip_path


class TestIsmagnetQuickImage:
    """Tests for MagnetQuickImageParser.is_magnet_quick_image()."""

    def test_valid_zip_with_adb_tar(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        assert MagnetQuickImageParser.is_magnet_quick_image(zip_path) is True

    def test_zip_without_adb_tar(self, tmp_path):
        zip_path = os.path.join(str(tmp_path), "other.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("something.txt", "hello")
        assert MagnetQuickImageParser.is_magnet_quick_image(zip_path) is False

    def test_non_zip_file(self, tmp_path):
        path = os.path.join(str(tmp_path), "not_a_zip.txt")
        with open(path, 'w') as f:
            f.write("not a zip")
        assert MagnetQuickImageParser.is_magnet_quick_image(path) is False

    def test_directory_containing_zip(self, tmp_path):
        _make_magnet_zip(str(tmp_path))
        assert MagnetQuickImageParser.is_magnet_quick_image(str(tmp_path)) is True

    def test_directory_without_zip(self, tmp_path):
        assert MagnetQuickImageParser.is_magnet_quick_image(str(tmp_path)) is False

    def test_nonexistent_path(self):
        assert MagnetQuickImageParser.is_magnet_quick_image("/nonexistent/path") is False


class TestFindZipInDir:
    """Tests for MagnetQuickImageParser.find_zip_in_dir()."""

    def test_file_path_returns_same(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        assert MagnetQuickImageParser.find_zip_in_dir(zip_path) == zip_path

    def test_directory_finds_zip(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        found = MagnetQuickImageParser.find_zip_in_dir(str(tmp_path))
        assert found == zip_path

    def test_directory_without_zip_returns_none(self, tmp_path):
        assert MagnetQuickImageParser.find_zip_in_dir(str(tmp_path)) is None


class TestMagnetParse:
    """Tests for MagnetQuickImageParser.parse() with synthetic data."""

    def test_basic_parse(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        assert backup.backup_type == "android"
        assert backup.is_encrypted is False
        assert len(backup.files) > 0

    def test_adb_tar_files_parsed(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        domains = {f.domain for f in backup.files}
        assert "com.example" in domains
        assert "shared/0" in domains

    def test_directory_mode_bits_fixed(self, tmp_path):
        """Verify the mode bit fix: directory entries get 0o040000 set."""
        zip_path = _make_magnet_zip(str(tmp_path))
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        dirs = [f for f in backup.files if f.flags == 2]
        for d in dirs:
            assert d.is_directory is True, f"Directory {d.file_id} has mode {oct(d.mode)} — is_directory should be True"

    def test_android_version_from_manifest(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()
        assert backup.android_version == "SDK 28"

    def test_sdcard_deduplication(self, tmp_path):
        """sdcard entries already in adb-data shared/0 should be skipped."""
        adb = [
            ("shared/0/", None),
            ("shared/0/DCIM/", None),
            ("shared/0/DCIM/photo.jpg", b"jpeg data"),
        ]
        sdcard = [
            ("sdcard/DCIM/photo.jpg", b"jpeg data"),  # Duplicate — should be skipped
            ("sdcard/Download/file.pdf", b"pdf data"),  # Unique — should be added
        ]
        zip_path = _make_magnet_zip(str(tmp_path), adb_members=adb, sdcard_members=sdcard)
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        # Count shared/0 files (non-directory)
        shared_files = [f for f in backup.files if f.domain == "shared/0" and not f.is_directory]
        paths = [f.relative_path for f in shared_files]
        assert "DCIM/photo.jpg" in paths
        assert "Download/file.pdf" in paths
        # photo.jpg should appear only once
        assert paths.count("DCIM/photo.jpg") == 1

    def test_sdcard_unique_entries_added(self, tmp_path):
        """Unique sdcard entries should be added as shared/0."""
        adb = [("shared/0/", None)]
        sdcard = [
            ("sdcard/Download/", None),
            ("sdcard/Download/file.pdf", b"pdf data"),
        ]
        zip_path = _make_magnet_zip(str(tmp_path), adb_members=adb, sdcard_members=sdcard)
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        shared_files = [f for f in backup.files if f.domain == "shared/0"]
        paths = [f.relative_path for f in shared_files]
        assert "Download/file.pdf" in paths

    def test_live_data_entries(self, tmp_path):
        live = [
            ("dumpsys_battery.txt", b"battery info"),
            ("contacts2.db", b"contacts data"),
        ]
        zip_path = _make_magnet_zip(str(tmp_path), live_data=live)
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        live_files = [f for f in backup.files if f.domain == "Live Data"]
        assert len(live_files) == 2
        paths = {f.relative_path for f in live_files}
        assert "dumpsys_battery.txt" in paths
        assert "contacts2.db" in paths

    def test_no_sdcard_tar(self, tmp_path):
        """Parse succeeds when sdcard.tar.gz is absent."""
        zip_path = _make_magnet_zip(str(tmp_path), sdcard_members=None)
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()
        assert len(backup.files) > 0

    def test_progress_callback(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        parser = MagnetQuickImageParser(zip_path)
        calls = []
        backup = parser.parse(progress_callback=lambda cur, total, msg: calls.append((cur, total, msg)))
        assert len(calls) > 0
        # Last call should be 100%
        assert calls[-1][0] == 100


class TestMagnetGetFileContent:
    """Tests for MagnetQuickImageParser.get_file_content()."""

    def test_get_adb_tar_content(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        db_file = next(f for f in backup.files if f.relative_path == "db/data.db")
        content = MagnetQuickImageParser.get_file_content(backup, db_file)
        assert content == b"database content"

    def test_get_sdcard_content(self, tmp_path):
        adb = [("shared/0/", None)]
        sdcard = [("sdcard/unique.txt", b"unique content")]
        zip_path = _make_magnet_zip(str(tmp_path), adb_members=adb, sdcard_members=sdcard)
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        unique_file = next(f for f in backup.files if f.relative_path == "unique.txt")
        content = MagnetQuickImageParser.get_file_content(backup, unique_file)
        assert content == b"unique content"

    def test_get_live_data_content(self, tmp_path):
        live = [("info.txt", b"live info")]
        zip_path = _make_magnet_zip(str(tmp_path), live_data=live)
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        live_file = next(f for f in backup.files if f.domain == "Live Data" and f.relative_path == "info.txt")
        content = MagnetQuickImageParser.get_file_content(backup, live_file)
        assert content == b"live info"

    def test_directory_returns_none(self, tmp_path):
        zip_path = _make_magnet_zip(str(tmp_path))
        parser = MagnetQuickImageParser(zip_path)
        backup = parser.parse()

        dir_entry = next(f for f in backup.files if f.is_directory)
        content = MagnetQuickImageParser.get_file_content(backup, dir_entry)
        assert content is None
