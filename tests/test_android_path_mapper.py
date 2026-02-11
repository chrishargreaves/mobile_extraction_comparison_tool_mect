"""Tests for android_path_mapper module."""

import pytest

from android_backup_parser import AndroidBackupFile, AndroidBackup
from android_path_mapper import AndroidPathMapper
from path_mapper import MappingStatus
from filesystem_loader import FilesystemFile, FilesystemAcquisition
from ios_backup_parser import ParsingLog


def _make_mapper(backup_files, fs_files=None, platform="android"):
    """Helper to create an AndroidPathMapper with synthetic data."""
    backup = AndroidBackup(
        path="/fake.ab",
        files=backup_files,
        manifest_db_row_count=len(backup_files),
    )
    acq = FilesystemAcquisition(
        path="/fake/fs",
        format="tar",
        platform=platform,
        files=fs_files or [],
    )
    acq.build_index()
    return AndroidPathMapper(backup, acq)


def _file(domain, token, rel, mode=0o100644, file_size=1024):
    """Shorthand for creating a non-directory AndroidBackupFile."""
    file_id = f"apps/{domain}/{token}/{rel}" if token else f"apps/{domain}"
    return AndroidBackupFile(
        file_id=file_id,
        domain=domain,
        relative_path=f"{token}/{rel}" if token and rel else (token or rel or ""),
        file_size=file_size,
        mode=mode,
        token=token,
        flags=1,
    )


def _dir(domain, token, rel=""):
    """Shorthand for creating a directory AndroidBackupFile."""
    return AndroidBackupFile(
        file_id=f"apps/{domain}/{token}/{rel}" if rel else f"apps/{domain}/{token}",
        domain=domain,
        relative_path=f"{token}/{rel}" if rel else token,
        file_size=0,
        mode=0o040755,
        token=token,
        flags=2,
    )


class TestMapBackupFile:
    """Tests for AndroidPathMapper._map_backup_file()."""

    def test_unmappable_manifest(self):
        bf = _file("com.example", "_manifest", "")
        mapper = _make_mapper([bf])
        path, notes = mapper._map_backup_file(bf)
        assert path is None
        assert "_manifest" in notes

    def test_unmappable_k_token(self):
        bf = _file("com.example", "k", "")
        mapper = _make_mapper([bf])
        path, notes = mapper._map_backup_file(bf)
        assert path is None
        assert "'k'" in notes

    def test_live_data_unmappable(self):
        bf = AndroidBackupFile(
            file_id="zip:Live Data/info.txt",
            domain="Live Data",
            relative_path="info.txt",
            file_size=100,
            mode=0o100644,
            token="",
            flags=1,
        )
        mapper = _make_mapper([bf])
        path, notes = mapper._map_backup_file(bf)
        assert path is None
        assert "Live Data" in notes

    def test_shared_storage(self):
        bf = AndroidBackupFile(
            file_id="shared/0/DCIM/photo.jpg",
            domain="shared/0",
            relative_path="DCIM/photo.jpg",
            file_size=2048,
            mode=0o100644,
            token="",
            flags=1,
        )
        mapper = _make_mapper([bf])
        path, notes = mapper._map_backup_file(bf)
        assert path == "/data/media/0/DCIM/photo.jpg"

    def test_shared_storage_root(self):
        bf = AndroidBackupFile(
            file_id="shared/0",
            domain="shared/0",
            relative_path="",
            file_size=0,
            mode=0o040755,
            token="",
            flags=2,
        )
        mapper = _make_mapper([bf])
        path, notes = mapper._map_backup_file(bf)
        assert path == "/data/media/0"

    def test_db_token(self):
        bf = _file("com.example", "db", "data.db")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/data/data/com.example/databases/data.db"

    def test_sp_token(self):
        bf = _file("com.example", "sp", "prefs.xml")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/data/data/com.example/shared_prefs/prefs.xml"

    def test_r_token(self):
        bf = _file("com.example", "r", "file.txt")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/data/data/com.example/file.txt"

    def test_f_token(self):
        bf = _file("com.example", "f", "myfile.txt")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/data/data/com.example/files/myfile.txt"

    def test_ef_token(self):
        bf = _file("com.example", "ef", "data.txt")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/storage/emulated/0/Android/data/com.example/files/data.txt"

    def test_c_token(self):
        bf = _file("com.example", "c", "cache.tmp")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/data/data/com.example/cache/cache.tmp"

    def test_nb_token(self):
        bf = _file("com.example", "nb", "nobackup.dat")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/data/data/com.example/no_backup/nobackup.dat"

    def test_d_db_token(self):
        bf = _file("com.example", "d_db", "de.db")
        mapper = _make_mapper([bf])
        path, _ = mapper._map_backup_file(bf)
        assert path == "/data/user_de/0/com.example/databases/de.db"

    def test_apk_token_with_resolution(self):
        bf = _file("com.example", "a", "base.apk")
        fs_files = [
            FilesystemFile("/data/app/com.example-abc123/base.apk", 5000, False, platform="android"),
        ]
        mapper = _make_mapper([bf], fs_files)
        path, notes = mapper._map_backup_file(bf)
        assert path == "/data/app/com.example-abc123/base.apk"
        assert "resolved" in notes.lower()

    def test_apk_token_without_resolution(self):
        bf = _file("com.example", "a", "base.apk")
        mapper = _make_mapper([bf])  # No filesystem files
        path, notes = mapper._map_backup_file(bf)
        assert path == "/data/app/com.example/base.apk"
        assert "fallback" in notes.lower()

    def test_unknown_token(self):
        bf = _file("com.example", "zzz", "data.txt")
        mapper = _make_mapper([bf])
        path, notes = mapper._map_backup_file(bf)
        assert path is None
        assert "Unknown token" in notes

    def test_empty_token_package_root(self):
        bf = AndroidBackupFile(
            file_id="apps/com.example",
            domain="com.example",
            relative_path="",
            file_size=0,
            mode=0o040755,
            token="",
            flags=2,
        )
        mapper = _make_mapper([bf])
        path, notes = mapper._map_backup_file(bf)
        assert path is None
        assert "Package root" in notes


class TestResolveApkDir:
    """Tests for AndroidPathMapper._resolve_apk_dir()."""

    def test_found(self):
        fs_files = [
            FilesystemFile("/data/app/com.example-abc123/base.apk", 5000, False, platform="android"),
        ]
        mapper = _make_mapper([], fs_files)
        result = mapper._resolve_apk_dir("com.example")
        assert result == "com.example-abc123"

    def test_not_found(self):
        mapper = _make_mapper([], [])
        result = mapper._resolve_apk_dir("com.missing")
        assert result is None

    def test_caching(self):
        fs_files = [
            FilesystemFile("/data/app/com.example-xyz/base.apk", 5000, False, platform="android"),
        ]
        mapper = _make_mapper([], fs_files)

        result1 = mapper._resolve_apk_dir("com.example")
        assert result1 == "com.example-xyz"

        # Second call should use cache
        result2 = mapper._resolve_apk_dir("com.example")
        assert result2 == result1
        assert "com.example" in mapper._apk_dir_cache


class TestMapAll:
    """Tests for AndroidPathMapper.map_all() end-to-end."""

    def test_mixed_results(self):
        backup_files = [
            _file("com.example", "db", "data.db"),
            _file("com.example", "_manifest", ""),
            _file("com.missing", "r", "file.txt"),
        ]
        fs_files = [
            FilesystemFile("/data/data/com.example/databases/data.db", 1024, False, platform="android"),
            FilesystemFile("/data/data/com.other/files/extra.txt", 500, False, platform="android"),
        ]
        mapper = _make_mapper(backup_files, fs_files)
        mappings = mapper.map_all()

        assert mapper.statistics.mapped_files == 1  # db/data.db found
        assert mapper.statistics.unmappable_files == 1  # _manifest
        assert mapper.statistics.not_found_files == 1  # com.missing/r/file.txt
        assert mapper.statistics.total_backup_files == 3
        assert mapper.statistics.total_filesystem_files == 2

    def test_coverage_calculation(self):
        bf = _file("com.example", "db", "data.db")
        fs_files = [
            FilesystemFile("/data/data/com.example/databases/data.db", 1024, False, platform="android"),
            FilesystemFile("/data/data/com.example/databases/other.db", 512, False, platform="android"),
        ]
        mapper = _make_mapper([bf], fs_files)
        mapper.map_all()

        # 1 mapped out of 2 filesystem files = 50%
        assert mapper.statistics.backup_coverage_percent == pytest.approx(50.0)

    def test_directories_skipped_in_mapping(self):
        files = [
            _dir("com.example", "db"),  # Directory — should be skipped
            _file("com.example", "db", "data.db"),  # File — should be mapped
        ]
        mapper = _make_mapper(files)
        mappings = mapper.map_all()

        # Only the file should produce a mapping
        assert len(mappings) == 1
        assert mappings[0].backup_file.relative_path == "db/data.db"

    def test_filesystem_only_files(self):
        mapper = _make_mapper(
            [],  # No backup files
            [FilesystemFile("/data/extra.txt", 100, False, platform="android")],
        )
        mapper.map_all()
        assert mapper.statistics.filesystem_only_files == 1

    def test_path_equivalence_lookup(self):
        """Mapper should find files via /data/data/ <-> /data/user/0/ equivalence."""
        bf = _file("com.example", "r", "file.txt")
        fs_files = [
            # Filesystem has it under /data/user/0/ not /data/data/
            FilesystemFile("/data/user/0/com.example/file.txt", 1024, False, platform="android"),
        ]
        mapper = _make_mapper([bf], fs_files)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 1


class TestMappingsByDomain:
    """Tests for AndroidPathMapper helper methods."""

    def test_get_mappings_by_domain(self):
        files = [
            _file("com.a", "db", "a.db"),
            _file("com.a", "sp", "prefs.xml"),
            _file("com.b", "r", "file.txt"),
        ]
        mapper = _make_mapper(files)
        mapper.map_all()

        by_domain = mapper.get_mappings_by_domain()
        assert len(by_domain["com.a"]) == 2
        assert len(by_domain["com.b"]) == 1

    def test_get_unmapped_backup_files(self):
        files = [
            _file("com.example", "_manifest", ""),
            _file("com.missing", "r", "file.txt"),
        ]
        mapper = _make_mapper(files)
        mapper.map_all()

        unmapped = mapper.get_unmapped_backup_files()
        assert len(unmapped) == 2

    def test_get_filesystem_files_not_in_backup(self):
        fs_files = [
            FilesystemFile("/data/data/com.other/databases/other.db", 500, False, platform="android"),
        ]
        mapper = _make_mapper([], fs_files)
        mapper.map_all()

        fs_only = mapper.get_filesystem_files_not_in_backup()
        assert len(fs_only) == 1
        assert fs_only[0].path == "/data/data/com.other/databases/other.db"


class TestCoverageCalculations:
    """Tests that coverage statistics are calculated correctly."""

    def test_all_mapped(self):
        """When every backup file maps to a filesystem file, coverage = 100%."""
        backup_files = [
            _file("com.a", "db", "a.db"),
            _file("com.a", "sp", "prefs.xml"),
        ]
        fs_files = [
            FilesystemFile("/data/data/com.a/databases/a.db", 1024, False, platform="android"),
            FilesystemFile("/data/data/com.a/shared_prefs/prefs.xml", 512, False, platform="android"),
        ]
        mapper = _make_mapper(backup_files, fs_files)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 2
        assert mapper.statistics.not_found_files == 0
        assert mapper.statistics.unmappable_files == 0
        assert mapper.statistics.backup_only_files == 0
        assert mapper.statistics.filesystem_only_files == 0
        assert mapper.statistics.backup_coverage_percent == pytest.approx(100.0)

    def test_no_backup_files(self):
        """When backup is empty, coverage should be 0%."""
        fs_files = [
            FilesystemFile("/data/data/com.a/databases/a.db", 1024, False, platform="android"),
        ]
        mapper = _make_mapper([], fs_files)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 0
        assert mapper.statistics.backup_coverage_percent == pytest.approx(0.0)
        assert mapper.statistics.filesystem_only_files == 1

    def test_no_filesystem_files(self):
        """When filesystem is empty, coverage stays 0% (no division by zero)."""
        bf = _file("com.a", "db", "a.db")
        mapper = _make_mapper([bf], [])
        mapper.map_all()

        assert mapper.statistics.backup_coverage_percent == pytest.approx(0.0)
        assert mapper.statistics.not_found_files == 1
        assert mapper.statistics.backup_only_files == 1

    def test_mixed_statuses(self):
        """Verify counts and percentages with mapped, not_found, and unmappable."""
        backup_files = [
            _file("com.a", "db", "a.db"),          # will map
            _file("com.missing", "r", "file.txt"),  # not found
            _file("com.b", "_manifest", ""),         # unmappable
        ]
        fs_files = [
            FilesystemFile("/data/data/com.a/databases/a.db", 1024, False, platform="android"),
            FilesystemFile("/data/data/com.a/databases/extra.db", 256, False, platform="android"),
        ]
        mapper = _make_mapper(backup_files, fs_files)
        mapper.map_all()

        assert mapper.statistics.total_backup_files == 3
        assert mapper.statistics.total_filesystem_files == 2
        assert mapper.statistics.mapped_files == 1
        assert mapper.statistics.not_found_files == 1
        assert mapper.statistics.unmappable_files == 1
        assert mapper.statistics.backup_only_files == 2
        assert mapper.statistics.filesystem_only_files == 1
        # Coverage = 1 mapped / 2 filesystem = 50%
        assert mapper.statistics.backup_coverage_percent == pytest.approx(50.0)

    def test_backup_only_is_sum_of_notfound_and_unmappable(self):
        """backup_only_files should always equal not_found + unmappable."""
        backup_files = [
            _file("com.a", "r", "a.txt"),
            _file("com.b", "r", "b.txt"),
            _file("com.c", "_manifest", ""),
            _file("com.d", "zzz", "x.txt"),
        ]
        mapper = _make_mapper(backup_files, [])
        mapper.map_all()

        assert mapper.statistics.backup_only_files == (
            mapper.statistics.not_found_files + mapper.statistics.unmappable_files
        )
