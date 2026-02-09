"""Shared fixtures for MECT unit tests."""

import sys
import os
import pytest

# Add project root to path so we can import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from android_backup_parser import AndroidBackupFile, AndroidBackup
from ios_backup_parser import BackupFile, ParsingLog
from filesystem_loader import FilesystemFile, FilesystemAcquisition


@pytest.fixture
def make_android_file():
    """Factory fixture for creating AndroidBackupFile instances."""
    def _make(
        file_id="apps/com.example/db/data.db",
        domain="com.example",
        relative_path="db/data.db",
        file_size=1024,
        mode=0o100644,
        modified_time=None,
        flags=1,
        actual_file_size=None,
        token="db",
    ):
        return AndroidBackupFile(
            file_id=file_id,
            domain=domain,
            relative_path=relative_path,
            file_size=file_size,
            mode=mode,
            modified_time=modified_time,
            flags=flags,
            actual_file_size=actual_file_size,
            token=token,
        )
    return _make


@pytest.fixture
def make_ios_file():
    """Factory fixture for creating BackupFile instances."""
    def _make(
        file_id="abc123def456",
        domain="HomeDomain",
        relative_path="Library/SMS/sms.db",
        file_size=1024,
        mode=0o100644,
        modified_time=None,
        flags=1,
        actual_file_size=None,
    ):
        return BackupFile(
            file_id=file_id,
            domain=domain,
            relative_path=relative_path,
            file_size=file_size,
            mode=mode,
            modified_time=modified_time,
            flags=flags,
            actual_file_size=actual_file_size,
        )
    return _make


@pytest.fixture
def make_fs_file():
    """Factory fixture for creating FilesystemFile instances."""
    def _make(
        path="/data/data/com.example/databases/data.db",
        size=1024,
        is_directory=False,
        modified_time=None,
        platform="android",
    ):
        return FilesystemFile(
            path=path,
            size=size,
            is_directory=is_directory,
            modified_time=modified_time,
            platform=platform,
        )
    return _make


@pytest.fixture
def make_filesystem(make_fs_file):
    """Factory fixture for creating FilesystemAcquisition with built index."""
    def _make(files=None, platform="android"):
        acq = FilesystemAcquisition(
            path="/fake/path",
            format="tar",
            platform=platform,
            files=files or [],
        )
        acq.build_index()
        return acq
    return _make


@pytest.fixture
def make_android_backup(make_android_file):
    """Factory fixture for creating AndroidBackup instances."""
    def _make(files=None):
        return AndroidBackup(
            path="/fake/backup.ab",
            files=files or [],
            manifest_db_row_count=len(files) if files else 0,
        )
    return _make
