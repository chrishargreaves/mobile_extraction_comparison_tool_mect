"""Tests for ios_backup_parser module."""

import io
import os
import plistlib
import sqlite3
import zipfile

import pytest

from ios_backup_parser import BackupFile, ParsingLog, ParsingLogEntry, iOSBackupParser


class TestBackupFileIsDirectory:
    """Tests for BackupFile.is_directory — multi-condition logic."""

    def test_directory_by_mode(self):
        f = BackupFile("abc", "HomeDomain", "Library", 0, mode=0o040755, flags=2)
        assert f.is_directory is True

    def test_directory_by_flags_only(self):
        """flags=2 indicates directory even with mode=0."""
        f = BackupFile("abc", "HomeDomain", "Library", 0, mode=0, flags=2)
        assert f.is_directory is True

    def test_directory_by_fallback(self):
        """mode=0, file_size=0, empty file_id → directory."""
        f = BackupFile("", "HomeDomain", "Library", 0, mode=0, flags=0)
        assert f.is_directory is True

    def test_regular_file(self):
        f = BackupFile("abc123", "HomeDomain", "Library/SMS/sms.db", 1024, mode=0o100644, flags=1)
        assert f.is_directory is False

    def test_file_with_zero_size(self):
        """Zero-size file with valid file_id is still a file, not a directory."""
        f = BackupFile("abc123", "HomeDomain", "empty.txt", 0, mode=0o100644, flags=1)
        assert f.is_directory is False

    def test_file_with_mode_zero_but_has_id(self):
        """mode=0, flags=1 with file_id → not a directory (fallback requires empty file_id)."""
        f = BackupFile("abc123", "HomeDomain", "file.txt", 0, mode=0, flags=1)
        assert f.is_directory is False

    def test_symlink_mode(self):
        f = BackupFile("abc", "HomeDomain", "link", 0, mode=0o120777, flags=1)
        assert f.is_directory is False


class TestParsingLogAddEntry:
    """Tests for ParsingLog.add_entry() counter logic."""

    def test_add_file_increments_counter(self):
        log = ParsingLog()
        log.add_entry("f1", "HomeDomain", "file.txt", "added_file", manifest_size=100)
        assert log.files_added == 1
        assert log.directories_added == 0
        assert log.skipped_no_content == 0
        assert log.errors == 0
        assert len(log.entries) == 1

    def test_add_directory_increments_counter(self):
        log = ParsingLog()
        log.add_entry("d1", "HomeDomain", "Library", "added_directory")
        assert log.directories_added == 1
        assert log.files_added == 0

    def test_add_skipped_increments_counter(self):
        log = ParsingLog()
        log.add_entry("s1", "HomeDomain", "missing.txt", "skipped_no_content")
        assert log.skipped_no_content == 1

    def test_add_error_increments_counter(self):
        log = ParsingLog()
        log.add_entry("e1", "HomeDomain", "bad.txt", "error", details="parse failed")
        assert log.errors == 1

    def test_multiple_entries(self):
        log = ParsingLog()
        log.add_entry("f1", "Dom", "a.txt", "added_file")
        log.add_entry("f2", "Dom", "b.txt", "added_file")
        log.add_entry("d1", "Dom", "dir", "added_directory")
        log.add_entry("e1", "Dom", "bad", "error")
        assert log.files_added == 2
        assert log.directories_added == 1
        assert log.errors == 1
        assert len(log.entries) == 4

    def test_entry_indexed_by_file_id(self):
        log = ParsingLog()
        log.add_entry("unique_id", "Dom", "path", "added_file")
        assert "unique_id" in log._entry_by_file_id


class TestParsingLogUpdateActualSize:
    """Tests for ParsingLog.update_actual_size() — size mismatch detection."""

    def test_matching_size(self):
        log = ParsingLog()
        log.add_entry("f1", "Dom", "file.txt", "added_file", manifest_size=100)
        log.update_actual_size("f1", 100)
        assert log.size_mismatches == 0

    def test_mismatching_size(self):
        log = ParsingLog()
        log.add_entry("f1", "Dom", "file.txt", "added_file", manifest_size=100)
        log.update_actual_size("f1", 200)
        assert log.size_mismatches == 1

    def test_manifest_zero_actual_nonzero(self):
        log = ParsingLog()
        log.add_entry("f1", "Dom", "file.txt", "added_file", manifest_size=0)
        log.update_actual_size("f1", 500)
        assert log.size_mismatches == 1
        assert log.manifest_size_zero == 1

    def test_unknown_file_id(self):
        """Updating a non-existent file_id should not crash."""
        log = ParsingLog()
        log.update_actual_size("nonexistent", 100)
        assert log.size_mismatches == 0

    def test_actual_size_none(self):
        log = ParsingLog()
        log.add_entry("f1", "Dom", "file.txt", "added_file", manifest_size=100)
        log.update_actual_size("f1", None)
        assert log.size_mismatches == 0


class TestParsingLogToText:
    """Tests for ParsingLog.to_text() output format."""

    def test_basic_output(self):
        log = ParsingLog()
        log.timestamp = "2026-02-09T12:00:00"
        log.total_rows = 2
        log.add_entry("f1", "HomeDomain", "file.txt", "added_file", manifest_size=100)
        log.add_entry("d1", "HomeDomain", "Library", "added_directory")

        text = log.to_text()
        assert "Manifest.db Parsing Log" in text
        assert "2026-02-09T12:00:00" in text
        assert "Files added: 1" in text
        assert "Directories added: 1" in text
        assert "HomeDomain/file.txt" in text

    def test_size_mismatch_flagged(self):
        log = ParsingLog()
        log.add_entry("f1", "Dom", "file.txt", "added_file", manifest_size=100)
        log.update_actual_size("f1", 200)

        text = log.to_text()
        assert "SIZE MISMATCH" in text
        assert "manifest=100" in text
        assert "actual=200" in text


def _make_ios_magnet_zip(tmpdir, fs_entries=None, live_entries=None, backup_files=None):
    """Create a synthetic iOS Magnet Quick Image ZIP.

    Args:
        tmpdir: directory to write the ZIP in
        fs_entries: list of (path_after_Filesystem, content_bytes_or_None_for_dir)
        live_entries: list of (path_after_LiveData, content_bytes)
        backup_files: list of (sha1_id, domain, relative_path, content_bytes) for backup
    """
    zip_path = os.path.join(str(tmpdir), "Quick Image.zip")

    # Create a minimal Manifest.db in memory
    db_buf = io.BytesIO()
    db_path = os.path.join(str(tmpdir), "_tmp_manifest.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE Files (
        fileID TEXT, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB
    )""")

    if backup_files:
        for sha1, domain, rel, content in backup_files:
            # Create a minimal plist blob for the file metadata
            meta = plistlib.dumps({
                '$archiver': 'NSKeyedArchiver',
                '$objects': ['$null', {'Size': len(content), 'Mode': 0o100644}],
                '$top': {'root': 1},
                '$version': 100000,
            })
            conn.execute(
                "INSERT INTO Files VALUES (?, ?, ?, ?, ?)",
                (sha1, domain, rel, 1, meta),
            )
    conn.commit()

    with open(db_path, 'rb') as f:
        db_bytes = f.read()
    conn.close()
    os.unlink(db_path)

    # Create Manifest.plist
    manifest_plist = plistlib.dumps({
        'IsEncrypted': False,
        'Lockdown': {
            'DeviceName': 'Test iPhone',
            'ProductType': 'iPhone9,1',
            'ProductVersion': '13.3.1',
            'SerialNumber': 'TEST123',
            'UniqueDeviceID': 'test-udid',
        },
    })

    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('Manifest.db', db_bytes)
        zf.writestr('Manifest.plist', manifest_plist)
        zf.writestr('Status.plist', plistlib.dumps({'BackupState': 2, 'IsFullBackup': False}))

        # Add backup file content
        if backup_files:
            for sha1, domain, rel, content in backup_files:
                zf.writestr(f'{sha1[:2]}/{sha1}', content)

        # Add Filesystem/ entries
        if fs_entries:
            for path, content in fs_entries:
                if content is None:
                    zf.writestr(f'Filesystem/{path}/', '')
                else:
                    zf.writestr(f'Filesystem/{path}', content)

        # Add Live Data/ entries
        if live_entries:
            zf.writestr('Live Data/', '')
            for path, content in live_entries:
                zf.writestr(f'Live Data/{path}', content)

    return zip_path


class TestMagnetIOSFilesystemEntries:
    """Tests for Magnet iOS Quick Image Filesystem/ augmentation."""

    def test_filesystem_entries_added_as_filesystem_domain(self, tmp_path):
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            fs_entries=[
                ("DCIM/100APPLE", None),
                ("DCIM/100APPLE/IMG_0001.JPG", b"jpeg data"),
            ],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        fs_files = [f for f in backup.files if f.domain == "Filesystem"]
        assert len(fs_files) >= 1
        paths = [f.relative_path for f in fs_files]
        assert "DCIM/100APPLE/IMG_0001.JPG" in paths

    def test_filesystem_directories_detected(self, tmp_path):
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            fs_entries=[("DCIM", None)],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        dirs = [f for f in backup.files if f.domain == "Filesystem" and f.is_directory]
        assert any("DCIM" in f.relative_path for f in dirs)

    def test_live_data_skipped(self, tmp_path):
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            live_entries=[("device_properties.txt", b"device info")],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        # No Live Data files should be in the backup file list
        live_files = [f for f in backup.files if f.domain == "Live Data"]
        assert len(live_files) == 0

        # But they should be logged as skipped
        skipped = [e for e in backup.parsing_log.entries if 'Live Data' in e.details]
        assert len(skipped) >= 1

    def test_no_extras_for_plain_backup(self, tmp_path):
        """A plain iOS backup ZIP (no Filesystem/ or Live Data/) should not be affected."""
        zip_path = _make_ios_magnet_zip(tmp_path)
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        media_magnet = [f for f in backup.files if f.file_id.startswith("magnet_fs:")]
        assert len(media_magnet) == 0

    def test_filesystem_entries_separate_from_backup(self, tmp_path):
        """Filesystem/ entries should appear under Filesystem domain, not MediaDomain."""
        sha1 = "abcdef1234567890abcdef1234567890abcdef12"
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            backup_files=[(sha1, "MediaDomain", "Media/DCIM/100APPLE/IMG_0001.JPG", b"jpeg data")],
            fs_entries=[("DCIM/100APPLE/IMG_0001.JPG", b"jpeg data")],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        # The backup file should be in MediaDomain
        media = [f for f in backup.files if f.domain == "MediaDomain" and not f.is_directory]
        assert len(media) == 1

        # The Filesystem entry should be in its own domain
        fs = [f for f in backup.files if f.domain == "Filesystem"
              and f.relative_path == "DCIM/100APPLE/IMG_0001.JPG"]
        assert len(fs) == 1

    def test_manifest_row_count_updated(self, tmp_path):
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            fs_entries=[
                ("Downloads/file.pdf", b"pdf data"),
                ("Recordings/note.m4a", b"audio data"),
            ],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        # The extra files should be counted in manifest_db_row_count
        magnet_files = [f for f in backup.files if f.file_id.startswith("magnet_fs:")]
        assert len(magnet_files) >= 2
        assert backup.manifest_db_row_count >= 2


class TestMagnetIOSGetFileContent:
    """Tests for get_file_content() with Magnet Filesystem/ entries."""

    def test_get_filesystem_content(self, tmp_path):
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            fs_entries=[("DCIM/100APPLE/IMG_0001.JPG", b"jpeg image data")],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        magnet_file = next(f for f in backup.files if f.file_id.startswith("magnet_fs:"))
        content = parser.get_file_content(backup, magnet_file)
        assert content == b"jpeg image data"

    def test_get_standard_backup_content(self, tmp_path):
        sha1 = "abcdef1234567890abcdef1234567890abcdef12"
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            backup_files=[(sha1, "HomeDomain", "Library/SMS/sms.db", b"sms data")],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        std_file = next(f for f in backup.files if f.file_id == sha1)
        content = parser.get_file_content(backup, std_file)
        assert content == b"sms data"

    def test_get_content_reopens_zip(self, tmp_path):
        """Content extraction should work even with a fresh parser instance."""
        zip_path = _make_ios_magnet_zip(
            tmp_path,
            fs_entries=[("test.txt", b"test content")],
        )
        parser = iOSBackupParser(zip_path)
        backup = parser.parse()

        magnet_file = next(f for f in backup.files if f.file_id.startswith("magnet_fs:"))

        # Create a fresh parser (simulating how main.py does content extraction)
        fresh_parser = iOSBackupParser(zip_path)
        content = fresh_parser.get_file_content(backup, magnet_file)
        assert content == b"test content"
