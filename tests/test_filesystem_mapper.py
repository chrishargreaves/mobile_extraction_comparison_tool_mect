"""Tests for filesystem_mapper module (filesystem-to-filesystem comparison)."""

import pytest

from filesystem_loader import FilesystemFile, FilesystemAcquisition
from filesystem_mapper import (
    extract_domain_from_path,
    FilesystemAsBackupFile,
    FilesystemAsBackup,
    FilesystemMapper,
)
from path_mapper import MappingStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fs_file(path, size=1024, is_dir=False, platform='android'):
    """Shorthand for creating a FilesystemFile."""
    return FilesystemFile(
        path=path,
        size=0 if is_dir else size,
        is_directory=is_dir,
        modified_time=1700000000.0,
        platform=platform,
    )


def _make_mapper(source_files, ref_files, platform='android'):
    """Helper to create a FilesystemMapper with synthetic data."""
    source_acq = FilesystemAcquisition(
        path="/fake/source.zip",
        format="zip",
        platform=platform,
        files=source_files,
    )
    ref_acq = FilesystemAcquisition(
        path="/fake/reference.tar",
        format="tar",
        platform=platform,
        files=ref_files,
    )
    ref_acq.build_index()
    backup = FilesystemAsBackup(source_acq)
    return FilesystemMapper(backup, ref_acq)


# ---------------------------------------------------------------------------
# extract_domain_from_path
# ---------------------------------------------------------------------------

class TestExtractDomainFromPath:
    """Tests for the domain extraction function."""

    def test_android_data_data(self):
        domain, rel = extract_domain_from_path(
            '/data/data/com.whatsapp/databases/msgstore.db', 'android'
        )
        assert domain == 'com.whatsapp'
        assert rel == 'databases/msgstore.db'

    def test_android_data_data_root(self):
        domain, rel = extract_domain_from_path(
            '/data/data/com.whatsapp', 'android'
        )
        assert domain == 'com.whatsapp'
        assert rel == ''

    def test_android_data_user_0(self):
        domain, rel = extract_domain_from_path(
            '/data/user/0/com.example.app/shared_prefs/prefs.xml', 'android'
        )
        assert domain == 'com.example.app'
        assert rel == 'shared_prefs/prefs.xml'

    def test_android_data_app(self):
        domain, rel = extract_domain_from_path(
            '/data/app/com.whatsapp-abc123/base.apk', 'android'
        )
        assert domain == 'com.whatsapp'
        assert rel == 'base.apk'

    def test_android_data_app_no_suffix(self):
        domain, rel = extract_domain_from_path(
            '/data/app/com.example/lib/arm64.so', 'android'
        )
        assert domain == 'com.example'
        assert rel == 'lib/arm64.so'

    def test_android_sdcard(self):
        domain, rel = extract_domain_from_path(
            '/sdcard/DCIM/Camera/photo.jpg', 'android'
        )
        assert domain == 'shared/0'
        assert rel == 'DCIM/Camera/photo.jpg'

    def test_android_storage_emulated(self):
        domain, rel = extract_domain_from_path(
            '/storage/emulated/0/WhatsApp/Media/voice.opus', 'android'
        )
        assert domain == 'shared/0'
        assert rel == 'WhatsApp/Media/voice.opus'

    def test_android_data_media(self):
        domain, rel = extract_domain_from_path(
            '/data/media/0/DCIM/photo.jpg', 'android'
        )
        assert domain == 'shared/0'
        assert rel == 'DCIM/photo.jpg'

    def test_ios_app_container(self):
        domain, rel = extract_domain_from_path(
            '/private/var/mobile/Containers/Data/Application/ABCD-1234/Documents/file.txt',
            'ios'
        )
        assert domain == 'AppContainer-ABCD-1234'
        assert rel == 'Documents/file.txt'

    def test_ios_app_group(self):
        domain, rel = extract_domain_from_path(
            '/private/var/mobile/Containers/Shared/AppGroup/EFGH-5678/data.db',
            'ios'
        )
        assert domain == 'AppGroup-EFGH-5678'
        assert rel == 'data.db'

    def test_ios_home_domain(self):
        domain, rel = extract_domain_from_path(
            '/private/var/mobile/Library/SMS/sms.db', 'ios'
        )
        assert domain == 'HomeDomain'
        assert rel == 'Library/SMS/sms.db'

    def test_fallback_generic(self):
        domain, rel = extract_domain_from_path('/system/build.prop', 'android')
        assert domain == 'system'
        assert rel == 'build.prop'

    def test_empty_path(self):
        domain, rel = extract_domain_from_path('', 'android')
        assert domain == ''
        assert rel == ''

    def test_single_component(self):
        domain, rel = extract_domain_from_path('/etc', 'android')
        assert domain == 'etc'
        assert rel == ''


# ---------------------------------------------------------------------------
# FilesystemAsBackupFile
# ---------------------------------------------------------------------------

class TestFilesystemAsBackupFile:
    """Tests for the FilesystemFile-to-BackupFile adapter."""

    def test_basic_properties(self):
        fs = _fs_file('/data/data/com.example/files/data.json', size=2048)
        bf = FilesystemAsBackupFile(fs, 'android')
        assert bf.domain == 'com.example'
        assert bf.relative_path == 'files/data.json'
        assert bf.file_size == 2048
        assert bf.actual_file_size == 2048
        assert bf.is_directory is False
        assert bf.file_id == '/data/data/com.example/files/data.json'

    def test_full_domain_path(self):
        fs = _fs_file('/data/data/com.example/files/data.json')
        bf = FilesystemAsBackupFile(fs, 'android')
        assert bf.full_domain_path == 'com.example/files/data.json'

    def test_directory(self):
        fs = _fs_file('/data/data/com.example/files', is_dir=True)
        bf = FilesystemAsBackupFile(fs, 'android')
        assert bf.is_directory is True
        assert bf.flags == 2

    def test_domain_only(self):
        fs = _fs_file('/sdcard', is_dir=True)
        bf = FilesystemAsBackupFile(fs, 'android')
        # sdcard as directory → domain='shared/0', rel=''
        # full_domain_path should just be the domain
        assert bf.full_domain_path == 'shared/0'


# ---------------------------------------------------------------------------
# FilesystemAsBackup
# ---------------------------------------------------------------------------

class TestFilesystemAsBackup:
    """Tests for the FilesystemAcquisition-to-Backup adapter."""

    def test_basic_properties(self):
        acq = FilesystemAcquisition(
            path="/path/to/extraction.zip",
            format="zip",
            platform="android",
            files=[_fs_file('/data/data/com.example/db.sqlite')],
        )
        backup = FilesystemAsBackup(acq)
        assert backup.backup_type == 'filesystem'
        assert backup.is_encrypted is False
        assert backup.device_name == 'extraction.zip'
        assert backup.platform == 'android'
        assert len(backup.files) == 1
        assert backup.files[0].domain == 'com.example'

    def test_ios_platform(self):
        acq = FilesystemAcquisition(
            path="/path/to/device.tar",
            format="tar",
            platform="ios",
            files=[_fs_file('/private/var/mobile/Library/sms.db', platform='ios')],
        )
        backup = FilesystemAsBackup(acq)
        assert backup.platform == 'ios'


# ---------------------------------------------------------------------------
# FilesystemMapper
# ---------------------------------------------------------------------------

class TestFilesystemMapper:
    """Tests for filesystem-to-filesystem comparison."""

    def test_exact_match(self):
        """Files with identical paths should map successfully."""
        source = [_fs_file('/data/data/com.example/db.sqlite')]
        ref = [_fs_file('/data/data/com.example/db.sqlite')]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 1
        assert mapper.statistics.not_found_files == 0
        assert mapper.statistics.unmappable_files == 0
        assert len(mapper.mappings) == 1
        assert mapper.mappings[0].status == MappingStatus.MAPPED

    def test_not_found(self):
        """Files in source but not reference should be NOT_FOUND."""
        source = [_fs_file('/data/data/com.example/secret.db')]
        ref = [_fs_file('/data/data/com.other/data.db')]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 0
        assert mapper.statistics.not_found_files == 1
        assert mapper.mappings[0].status == MappingStatus.NOT_FOUND

    def test_filesystem_only(self):
        """Files in reference but not source should appear in filesystem_only count."""
        source = [_fs_file('/data/data/com.example/a.db')]
        ref = [
            _fs_file('/data/data/com.example/a.db'),
            _fs_file('/data/data/com.example/b.db'),
        ]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 1
        assert mapper.statistics.filesystem_only_files == 1

    def test_directories_excluded(self):
        """Directories should not be mapped, only counted."""
        source = [
            _fs_file('/data/data/com.example', is_dir=True),
            _fs_file('/data/data/com.example/db.sqlite'),
        ]
        ref = [_fs_file('/data/data/com.example/db.sqlite')]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        assert mapper.statistics.total_backup_files == 1
        assert mapper.statistics.total_backup_directories == 1
        assert mapper.statistics.mapped_files == 1
        # Only the file should produce a mapping
        assert len(mapper.mappings) == 1

    def test_path_equivalence_sdcard(self):
        """Android path equivalences should be resolved by the filesystem index."""
        source = [_fs_file('/sdcard/DCIM/photo.jpg')]
        ref = [_fs_file('/storage/emulated/0/DCIM/photo.jpg')]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 1
        assert mapper.mappings[0].status == MappingStatus.MAPPED

    def test_path_equivalence_data_user(self):
        """/data/data/ and /data/user/0/ should be treated as equivalent."""
        source = [_fs_file('/data/data/com.example/db.sqlite')]
        ref = [_fs_file('/data/user/0/com.example/db.sqlite')]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        assert mapper.statistics.mapped_files == 1

    def test_coverage_percent(self):
        """Coverage should be mapped / total reference files * 100."""
        source = [_fs_file('/data/data/com.example/a.db')]
        ref = [
            _fs_file('/data/data/com.example/a.db'),
            _fs_file('/data/data/com.example/b.db'),
        ]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        assert mapper.statistics.backup_coverage_percent == 50.0

    def test_get_unmapped_backup_files(self):
        source = [
            _fs_file('/data/data/com.a/x.db'),
            _fs_file('/data/data/com.b/y.db'),
        ]
        ref = [_fs_file('/data/data/com.a/x.db')]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        unmapped = mapper.get_unmapped_backup_files()
        assert len(unmapped) == 1
        assert unmapped[0].domain == 'com.b'

    def test_get_filesystem_files_not_in_backup(self):
        source = [_fs_file('/data/data/com.a/x.db')]
        ref = [
            _fs_file('/data/data/com.a/x.db'),
            _fs_file('/data/data/com.a/y.db'),
        ]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        fs_only = mapper.get_filesystem_files_not_in_backup()
        assert len(fs_only) == 1
        assert fs_only[0].path == '/data/data/com.a/y.db'

    def test_get_mappings_by_domain(self):
        source = [
            _fs_file('/data/data/com.a/x.db'),
            _fs_file('/data/data/com.b/y.db'),
        ]
        ref = [
            _fs_file('/data/data/com.a/x.db'),
            _fs_file('/data/data/com.b/y.db'),
        ]
        mapper = _make_mapper(source, ref)
        mapper.map_all()

        by_domain = mapper.get_mappings_by_domain()
        assert 'com.a' in by_domain
        assert 'com.b' in by_domain
        assert len(by_domain['com.a']) == 1
        assert len(by_domain['com.b']) == 1

    def test_empty_source(self):
        """Empty source should produce no mappings."""
        mapper = _make_mapper([], [_fs_file('/data/data/com.a/x.db')])
        mapper.map_all()

        assert mapper.statistics.mapped_files == 0
        assert mapper.statistics.filesystem_only_files == 1

    def test_empty_reference(self):
        """Empty reference should mark all source files as NOT_FOUND."""
        mapper = _make_mapper([_fs_file('/data/data/com.a/x.db')], [])
        mapper.map_all()

        assert mapper.statistics.not_found_files == 1
        assert mapper.statistics.mapped_files == 0

    def test_ios_comparison(self):
        """iOS filesystem-to-filesystem comparison should work."""
        source = [_fs_file('/private/var/mobile/Library/SMS/sms.db', platform='ios')]
        ref = [_fs_file('/private/var/mobile/Library/SMS/sms.db', platform='ios')]
        mapper = _make_mapper(source, ref, platform='ios')
        mapper.map_all()

        assert mapper.statistics.mapped_files == 1
