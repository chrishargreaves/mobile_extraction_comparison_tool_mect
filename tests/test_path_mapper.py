"""Tests for path_mapper module (iOS path mapping)."""

import pytest

from ios_backup_parser import BackupFile, iOSBackup, ParsingLog
from path_mapper import PathMapper, MappingStatus, MappingStatistics
from filesystem_loader import FilesystemFile, FilesystemAcquisition


def _make_ios_mapper(backup_files, fs_files=None, container_mappings=None):
    """Helper to create a PathMapper with synthetic data."""
    backup = iOSBackup(
        path="/fake/backup",
        is_encrypted=False,
        files=backup_files,
        manifest_db_row_count=len(backup_files),
        parsing_log=ParsingLog(),
    )
    acq = FilesystemAcquisition(
        path="/fake/fs",
        format="tar",
        platform="ios",
        files=fs_files or [],
    )
    if container_mappings:
        for key, mapping in container_mappings.items():
            setattr(acq, key, mapping)
    acq.build_index()
    return PathMapper(backup, acq)


def _ios_file(domain, rel, mode=0o100644, file_size=1024, flags=1):
    """Shorthand for creating an iOS BackupFile."""
    return BackupFile(
        file_id="abc123",
        domain=domain,
        relative_path=rel,
        file_size=file_size,
        mode=mode,
        flags=flags,
    )


class TestParseDomain:
    """Tests for PathMapper._parse_domain()."""

    def test_simple_domain(self):
        mapper = _make_ios_mapper([])
        base, ident = mapper._parse_domain("HomeDomain")
        assert base == "HomeDomain"
        assert ident is None

    def test_app_domain(self):
        mapper = _make_ios_mapper([])
        base, ident = mapper._parse_domain("AppDomain-com.example.app")
        assert base == "AppDomain"
        assert ident == "com.example.app"

    def test_app_domain_group(self):
        mapper = _make_ios_mapper([])
        base, ident = mapper._parse_domain("AppDomainGroup-group.com.example")
        assert base == "AppDomainGroup"
        assert ident == "group.com.example"

    def test_sys_container_domain(self):
        mapper = _make_ios_mapper([])
        base, ident = mapper._parse_domain("SysContainerDomain-com.apple.something")
        assert base == "SysContainerDomain"
        assert ident == "com.apple.something"

    def test_domain_with_multiple_hyphens(self):
        """Only split on first hyphen."""
        mapper = _make_ios_mapper([])
        base, ident = mapper._parse_domain("AppDomain-com.example-test.app")
        assert base == "AppDomain"
        assert ident == "com.example-test.app"


class TestMapDomainPath:
    """Tests for PathMapper._map_domain_path()."""

    def test_home_domain(self):
        bf = _ios_file("HomeDomain", "Library/SMS/sms.db")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/mobile/Library/SMS/sms.db"

    def test_keychain_domain(self):
        bf = _ios_file("KeychainDomain", "keychain-2.db")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/Keychains/keychain-2.db"

    def test_camera_roll_domain(self):
        bf = _ios_file("CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/mobile/Media/DCIM/100APPLE/IMG_0001.JPG"

    def test_root_domain(self):
        bf = _ios_file("RootDomain", "Library/Preferences/com.apple.plist")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/root/Library/Preferences/com.apple.plist"

    def test_app_domain_with_guid(self):
        bf = _ios_file("AppDomain-com.example.app", "Documents/data.db")
        fs_files = [
            FilesystemFile(
                "/private/var/mobile/Containers/Data/Application/AAAA-BBBB/Documents/data.db",
                1024, False, platform="ios",
            ),
        ]
        mapper = _make_ios_mapper(
            [bf], fs_files,
            container_mappings={"app_container_mapping": {"com.example.app": "AAAA-BBBB"}},
        )
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/mobile/Containers/Data/Application/AAAA-BBBB/Documents/data.db"
        assert "AAAA-BBBB" in notes

    def test_app_domain_without_guid_fallback(self):
        bf = _ios_file("AppDomain-com.example.app", "Documents/data.db")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/mobile/Containers/Data/Application/com.example.app/Documents/data.db"
        assert "fallback" in notes.lower()

    def test_app_domain_group_with_guid(self):
        bf = _ios_file("AppDomainGroup-group.com.example", "Library/data.db")
        mapper = _make_ios_mapper(
            [bf], [],
            container_mappings={"group_container_mapping": {"group.com.example": "CCCC-DDDD"}},
        )
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/mobile/Containers/Shared/AppGroup/CCCC-DDDD/Library/data.db"

    def test_sys_container_domain_with_guid(self):
        bf = _ios_file("SysContainerDomain-com.apple.something", "data.db")
        mapper = _make_ios_mapper(
            [bf], [],
            container_mappings={"system_container_mapping": {"com.apple.something": "EEEE-FFFF"}},
        )
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/containers/Data/System/EEEE-FFFF/data.db"

    def test_sys_shared_container_domain(self):
        bf = _ios_file("SysSharedContainerDomain-com.apple.group", "Library/pref.plist")
        mapper = _make_ios_mapper(
            [bf], [],
            container_mappings={"system_group_mapping": {"com.apple.group": "GGGG-HHHH"}},
        )
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/containers/Shared/SystemGroup/GGGG-HHHH/Library/pref.plist"

    def test_filesystem_domain(self):
        """Magnet Filesystem domain maps to /private/var/mobile/Media/."""
        bf = _ios_file("Filesystem", "DCIM/100APPLE/IMG_0001.JPG")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/mobile/Media/DCIM/100APPLE/IMG_0001.JPG"

    def test_unknown_domain(self):
        bf = _ios_file("UnknownDomain", "file.txt")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path is None
        assert "Unknown domain" in notes

    def test_domain_without_relative_path(self):
        bf = _ios_file("HomeDomain", "")
        mapper = _make_ios_mapper([bf])
        path, notes = mapper._map_domain_path(bf)
        assert path == "/private/var/mobile"


class TestPathMapperMapAll:
    """Tests for PathMapper.map_all() end-to-end."""

    def test_mapped_file(self):
        bf = _ios_file("HomeDomain", "Library/SMS/sms.db")
        fs = FilesystemFile("/private/var/mobile/Library/SMS/sms.db", 1024, False, platform="ios")
        mapper = _make_ios_mapper([bf], [fs])
        mappings = mapper.map_all()

        assert len(mappings) == 1
        assert mappings[0].status == MappingStatus.MAPPED
        assert mapper.statistics.mapped_files == 1

    def test_not_found_file(self):
        bf = _ios_file("HomeDomain", "Library/Missing/file.db")
        mapper = _make_ios_mapper([bf], [])
        mappings = mapper.map_all()

        assert mappings[0].status == MappingStatus.NOT_FOUND
        assert mapper.statistics.not_found_files == 1

    def test_unmappable_domain(self):
        bf = _ios_file("UnknownDomain", "file.txt")
        mapper = _make_ios_mapper([bf])
        mappings = mapper.map_all()

        assert mappings[0].status == MappingStatus.UNMAPPABLE
        assert mapper.statistics.unmappable_files == 1

    def test_directories_skipped(self):
        files = [
            _ios_file("HomeDomain", "Library", mode=0o040755, flags=2, file_size=0),
            _ios_file("HomeDomain", "Library/SMS/sms.db"),
        ]
        mapper = _make_ios_mapper(files)
        mappings = mapper.map_all()

        # Only the file should be mapped, not the directory
        assert len(mappings) == 1
        assert mapper.statistics.total_backup_files == 1

    def test_coverage_percent(self):
        bf = _ios_file("HomeDomain", "Library/SMS/sms.db")
        fs_files = [
            FilesystemFile("/private/var/mobile/Library/SMS/sms.db", 1024, False, platform="ios"),
            FilesystemFile("/private/var/mobile/Library/other.db", 512, False, platform="ios"),
        ]
        mapper = _make_ios_mapper([bf], fs_files)
        mapper.map_all()

        assert mapper.statistics.backup_coverage_percent == pytest.approx(50.0)

    def test_filesystem_only_count(self):
        mapper = _make_ios_mapper(
            [],
            [FilesystemFile("/private/var/mobile/extra.txt", 100, False, platform="ios")],
        )
        mapper.map_all()
        assert mapper.statistics.filesystem_only_files == 1

    def test_ios_private_prefix_matching(self):
        """Filesystem file with /private/ prefix should match backup mapping."""
        bf = _ios_file("HomeDomain", "Library/SMS/sms.db")
        fs = FilesystemFile("/private/var/mobile/Library/SMS/sms.db", 1024, False, platform="ios")
        mapper = _make_ios_mapper([bf], [fs])
        mapper.map_all()

        assert mapper.statistics.mapped_files == 1
