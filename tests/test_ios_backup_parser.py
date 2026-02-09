"""Tests for ios_backup_parser module."""

import pytest

from ios_backup_parser import BackupFile, ParsingLog, ParsingLogEntry


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
