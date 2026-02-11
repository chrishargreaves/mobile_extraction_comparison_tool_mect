"""Tests for alex_parser module (ALEX UFED-style extraction parsing)."""

import io
import os
import tarfile
import tempfile
import zlib
import zipfile

import pytest

from alex_parser import ALEXParser
from android_backup_parser import AndroidBackupFile


def _build_ab_bytes(tar_members: dict, encrypted=False, compressed=True) -> bytes:
    """Build a minimal Android .ab file from a dict of {name: (content, is_dir)}.

    Args:
        tar_members: {member_name: (content_bytes, is_dir)}
        compressed: Whether to zlib-compress the tar payload
    """
    # Build tar in memory
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode='w:') as tf:
        for name, (content, is_dir) in tar_members.items():
            info = tarfile.TarInfo(name=name)
            if is_dir:
                info.type = tarfile.DIRTYPE
                info.mode = 0o040755
                info.size = 0
                tf.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.mode = 0o100644
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))

    tar_data = tar_buf.getvalue()

    # Build .ab header + payload
    header = b"ANDROID BACKUP\n"
    header += b"5\n"  # format version
    header += b"1\n" if compressed else b"0\n"  # compression flag
    header += b"none\n"  # encryption

    if compressed:
        payload = zlib.compress(tar_data)
    else:
        payload = tar_data

    return header + payload


def _build_alex_zip(
    ab_bytes: bytes,
    sdcard_files: dict = None,
    backup_sdcard_files: dict = None,
    ufd_content: str = None,
) -> str:
    """Build a temporary ALEX-style ZIP and return its path.

    Args:
        ab_bytes: The .ab file content
        sdcard_files: {relative_path: content_bytes} for sdcard/ entries
        backup_sdcard_files: {relative_path: content_bytes} for backup/sdcard/ entries
        ufd_content: Optional .ufd file content (written alongside ZIP)

    Returns:
        Path to the temporary ZIP file
    """
    tmpdir = tempfile.mkdtemp()
    zip_path = os.path.join(tmpdir, "Device.zip")

    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('backup/backup.ab', ab_bytes)

        if sdcard_files:
            for rel_path, content in sdcard_files.items():
                zf.writestr(f'sdcard/{rel_path}', content)

        if backup_sdcard_files:
            for rel_path, content in backup_sdcard_files.items():
                zf.writestr(f'backup/sdcard/{rel_path}', content)

    if ufd_content:
        ufd_path = os.path.join(tmpdir, "Device.ufd")
        with open(ufd_path, 'w') as f:
            f.write(ufd_content)

    return zip_path


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestIsALEXExtraction:

    def test_valid_zip(self):
        ab = _build_ab_bytes({'apps/com.example/r/': (b'', True)})
        zip_path = _build_alex_zip(ab)
        assert ALEXParser.is_alex_extraction(zip_path) is True

    def test_directory_with_zip(self):
        ab = _build_ab_bytes({'apps/com.example/r/': (b'', True)})
        zip_path = _build_alex_zip(ab)
        parent = os.path.dirname(zip_path)
        assert ALEXParser.is_alex_extraction(parent) is True

    def test_nested_directory(self):
        """ALEX nests in a timestamped subdirectory."""
        ab = _build_ab_bytes({'apps/com.example/r/': (b'', True)})
        zip_path = _build_alex_zip(ab)
        grandparent = tempfile.mkdtemp()
        subdir = os.path.join(grandparent, "timestamped_dir")
        os.makedirs(subdir, exist_ok=True)
        # Move zip to subdir
        new_path = os.path.join(subdir, os.path.basename(zip_path))
        os.rename(zip_path, new_path)
        assert ALEXParser.is_alex_extraction(grandparent) is True

    def test_zip_without_backup_ab(self):
        tmpdir = tempfile.mkdtemp()
        zip_path = os.path.join(tmpdir, "notbackup.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('sdcard/photo.jpg', b'fake')
        assert ALEXParser.is_alex_extraction(zip_path) is False

    def test_nonexistent_path(self):
        assert ALEXParser.is_alex_extraction('/nonexistent/path') is False

    def test_non_zip_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("not a zip")
        assert ALEXParser.is_alex_extraction(str(f)) is False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParse:

    def test_basic_parse(self):
        """Parse a minimal .ab with one app file."""
        ab = _build_ab_bytes({
            'apps/com.example/db/test.db': (b'database content', False),
            'apps/com.example/db/': (b'', True),
        })
        zip_path = _build_alex_zip(ab)
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        assert backup.backup_type == 'android'
        assert backup.is_encrypted is False
        assert len(backup.files) == 2

    def test_files_parsed_correctly(self):
        ab = _build_ab_bytes({
            'apps/com.whatsapp/db/msgstore.db': (b'whatsapp data', False),
        })
        zip_path = _build_alex_zip(ab)
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        files = [f for f in backup.files if not f.is_directory]
        assert len(files) == 1
        assert files[0].domain == 'com.whatsapp'
        assert files[0].token == 'db'
        assert 'msgstore.db' in files[0].relative_path

    def test_sdcard_entries_added(self):
        """Sdcard entries from the ZIP should be added as shared/0."""
        ab = _build_ab_bytes({
            'apps/com.example/r/': (b'', True),
        })
        zip_path = _build_alex_zip(ab, sdcard_files={
            'DCIM/photo.jpg': b'jpeg data',
        })
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        sdcard_files = [f for f in backup.files if f.domain == 'shared/0' and not f.is_directory]
        assert len(sdcard_files) == 1
        assert sdcard_files[0].relative_path == 'DCIM/photo.jpg'

    def test_backup_sdcard_entries_added(self):
        """Files from backup/sdcard/ should be added as shared/0."""
        ab = _build_ab_bytes({
            'apps/com.example/r/': (b'', True),
        })
        zip_path = _build_alex_zip(ab, backup_sdcard_files={
            'Download/file.apk': b'apk data',
        })
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        sdcard_files = [f for f in backup.files if f.domain == 'shared/0' and not f.is_directory]
        assert len(sdcard_files) == 1
        assert sdcard_files[0].relative_path == 'Download/file.apk'

    def test_sdcard_deduplication(self):
        """Sdcard entries already in .ab shared/0 should not be duplicated."""
        ab = _build_ab_bytes({
            'shared/0/DCIM/photo.jpg': (b'from backup', False),
        })
        zip_path = _build_alex_zip(ab, sdcard_files={
            'DCIM/photo.jpg': b'from sdcard',
        })
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        photo_files = [
            f for f in backup.files
            if not f.is_directory and 'photo.jpg' in f.relative_path
        ]
        assert len(photo_files) == 1  # Not duplicated

    def test_device_info_from_ufd(self):
        """Device info should be read from the .ufd file."""
        ab = _build_ab_bytes({'apps/com.example/r/': (b'', True)})
        ufd = (
            "[DeviceInfo]\n"
            "Model=Pixel 2\n"
            "Vendor=Google\n"
            "OS=9\n"
        )
        zip_path = _build_alex_zip(ab, ufd_content=ufd)
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        assert backup.device_name == 'Google Pixel 2'
        assert backup.android_version == 'Android 9'

    def test_device_info_fallback_to_manifest(self):
        """Without .ufd, Android version should come from _manifest."""
        # Build a manifest with SDK version on line 4
        manifest = "com.example\n1\n1\n28\n"
        ab = _build_ab_bytes({
            'apps/com.example/_manifest': (manifest.encode(), False),
            'apps/com.example/r/data.txt': (b'test', False),
        })
        zip_path = _build_alex_zip(ab)
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        assert backup.android_version == 'SDK 28'

    def test_uncompressed_ab(self):
        """Uncompressed .ab files should also work."""
        ab = _build_ab_bytes(
            {'apps/com.example/r/data.txt': (b'test', False)},
            compressed=False,
        )
        zip_path = _build_alex_zip(ab)
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        files = [f for f in backup.files if not f.is_directory]
        assert len(files) == 1


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

class TestGetFileContent:

    def test_get_ab_content(self):
        """Should extract file content from the .ab tar."""
        content = b'important database content'
        ab = _build_ab_bytes({
            'apps/com.example/db/test.db': (content, False),
        })
        zip_path = _build_alex_zip(ab)
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        db_file = [f for f in backup.files if not f.is_directory][0]
        result = parser.get_file_content(backup, db_file)
        assert result == content

    def test_get_sdcard_content(self):
        """Should extract sdcard file content from the ZIP."""
        content = b'jpeg photo data'
        ab = _build_ab_bytes({'apps/com.example/r/': (b'', True)})
        zip_path = _build_alex_zip(ab, sdcard_files={
            'DCIM/photo.jpg': content,
        })
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        photo = [f for f in backup.files if f.domain == 'shared/0' and not f.is_directory][0]
        result = parser.get_file_content(backup, photo)
        assert result == content

    def test_directory_returns_none(self):
        ab = _build_ab_bytes({'apps/com.example/r/': (b'', True)})
        zip_path = _build_alex_zip(ab)
        parser = ALEXParser(zip_path)
        backup = parser.parse()

        dir_file = [f for f in backup.files if f.is_directory][0]
        result = parser.get_file_content(backup, dir_file)
        assert result is None
