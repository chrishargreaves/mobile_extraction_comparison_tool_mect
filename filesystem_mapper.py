"""
Filesystem-to-Filesystem Mapper

Enables comparison of two filesystem acquisitions (ZIP, TAR, directory) by
wrapping a FilesystemAcquisition in adapter classes that satisfy the duck-type
contracts expected by BackupTreeView, MappingInfoPanel, and StatisticsPanel.

This supports plain archives from tools like ALEX (SD card extractions,
UFED-style logical+) that don't conform to iOS backup or Android .ab formats.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from filesystem_loader import FilesystemAcquisition, FilesystemFile
from ios_backup_parser import ParsingLog
from path_mapper import PathMapping, MappingStatus, MappingStatistics


def extract_domain_from_path(path: str, platform: str) -> Tuple[str, str]:
    """
    Extract a (domain, relative_path) from a filesystem path for tree grouping.

    For Android, groups by package name where possible.
    For iOS, groups by container or domain equivalent.
    Falls back to first path component otherwise.
    """
    parts = path.strip('/').split('/')
    if not parts or parts == ['']:
        return ('', '')

    if platform == 'android':
        # /data/data/<pkg>/... or /data/user/0/<pkg>/...
        if len(parts) >= 3 and parts[0] == 'data' and parts[1] == 'data':
            pkg = parts[2]
            rel = '/'.join(parts[3:]) if len(parts) > 3 else ''
            return (pkg, rel)
        if len(parts) >= 4 and parts[0] == 'data' and parts[1] == 'user':
            # /data/user/0/<pkg>/...
            pkg = parts[3]
            rel = '/'.join(parts[4:]) if len(parts) > 4 else ''
            return (pkg, rel)
        # /data/app/<pkg>-<suffix>/...
        if len(parts) >= 3 and parts[0] == 'data' and parts[1] == 'app':
            pkg = parts[2].rsplit('-', 1)[0]
            rel = '/'.join(parts[3:]) if len(parts) > 3 else ''
            return (pkg, rel)
        # Shared storage paths → shared/0
        if parts[0] == 'sdcard':
            rel = '/'.join(parts[1:]) if len(parts) > 1 else ''
            return ('shared/0', rel)
        if parts[0] == 'storage' and len(parts) >= 3 and parts[1] == 'emulated':
            # /storage/emulated/0/...
            rel = '/'.join(parts[3:]) if len(parts) > 3 else ''
            return ('shared/0', rel)
        if (parts[0] == 'data' and parts[1] == 'media'
                and len(parts) >= 3):
            # /data/media/0/...
            rel = '/'.join(parts[3:]) if len(parts) > 3 else ''
            return ('shared/0', rel)

    elif platform == 'ios':
        # /private/var/mobile/Containers/Data/Application/<GUID>/...
        stripped = parts
        if stripped and stripped[0] == 'private':
            stripped = stripped[1:]
        if (len(stripped) >= 6
                and stripped[:4] == ['var', 'mobile', 'Containers', 'Data']
                and stripped[4] == 'Application'):
            guid = stripped[5]
            rel = '/'.join(stripped[6:]) if len(stripped) > 6 else ''
            return (f'AppContainer-{guid}', rel)
        # /private/var/mobile/Containers/Shared/AppGroup/<GUID>/...
        if (len(stripped) >= 6
                and stripped[:4] == ['var', 'mobile', 'Containers', 'Shared']
                and stripped[4] == 'AppGroup'):
            guid = stripped[5]
            rel = '/'.join(stripped[6:]) if len(stripped) > 6 else ''
            return (f'AppGroup-{guid}', rel)
        # /private/var/mobile/...
        if len(stripped) >= 2 and stripped[0] == 'var' and stripped[1] == 'mobile':
            rel = '/'.join(stripped[2:]) if len(stripped) > 2 else ''
            return ('HomeDomain', rel)

    # Fallback: first path component as domain
    if len(parts) >= 2:
        return (parts[0], '/'.join(parts[1:]))
    return (parts[0], '')


class FilesystemAsBackupFile:
    """Wraps a FilesystemFile to duck-type as BackupFile/AndroidBackupFile."""

    def __init__(self, fs_file: FilesystemFile, platform: str):
        self._fs_file = fs_file
        self.domain, self.relative_path = extract_domain_from_path(
            fs_file.normalized_path, platform
        )
        self.file_id = fs_file.path
        self.file_size = fs_file.size
        self.actual_file_size = fs_file.size
        self.mode = 0o40755 if fs_file.is_directory else 0o100644
        self.modified_time = fs_file.modified_time or 0.0
        self.flags = 2 if fs_file.is_directory else 1

    @property
    def is_directory(self) -> bool:
        return self._fs_file.is_directory

    @property
    def full_domain_path(self) -> str:
        if self.relative_path:
            return f"{self.domain}/{self.relative_path}"
        return self.domain


class FilesystemAsBackup:
    """Wraps a FilesystemAcquisition to duck-type as a Backup object."""

    def __init__(self, acquisition: FilesystemAcquisition):
        self._acquisition = acquisition
        self.path = acquisition.path
        self.backup_type = 'filesystem'
        self.is_encrypted = False
        self.is_zipped = acquisition.format == 'zip'
        self.ios_version = None
        self.android_version = None
        self.platform = acquisition.platform
        self.parsing_log = ParsingLog()

        # Derive a device name from the archive filename
        self.device_name = os.path.basename(acquisition.path)

        # Wrap all files
        self.files: List[FilesystemAsBackupFile] = [
            FilesystemAsBackupFile(f, acquisition.platform)
            for f in acquisition.files
        ]


class FilesystemMapper:
    """Compares a source filesystem acquisition against a reference filesystem acquisition."""

    def __init__(self, backup: FilesystemAsBackup, filesystem: FilesystemAcquisition):
        self.backup = backup
        self.filesystem = filesystem
        self.mappings: List[PathMapping] = []
        self.statistics = MappingStatistics()

    def map_all(self) -> List[PathMapping]:
        """Map source files to reference filesystem by normalized path."""
        self.mappings = []
        self.statistics = MappingStatistics()

        # Ensure reference index is built
        self.filesystem.build_index()

        # Count totals
        source_files = [f for f in self.backup.files if not f.is_directory]
        source_dirs = [f for f in self.backup.files if f.is_directory]
        ref_files = [f for f in self.filesystem.files if not f.is_directory]
        ref_dirs = [f for f in self.filesystem.files if f.is_directory]

        self.statistics.total_backup_files = len(source_files)
        self.statistics.total_backup_directories = len(source_dirs)
        self.statistics.total_filesystem_files = len(ref_files)
        self.statistics.total_filesystem_directories = len(ref_dirs)
        self.statistics.manifest_db_row_count = len(self.backup.files)

        mapped = 0
        not_found = 0

        # Track which reference files get matched
        matched_ref_paths = set()

        for bf in source_files:
            # Use the underlying FilesystemFile's normalized path for lookup
            fs_path = bf._fs_file.normalized_path
            match = self.filesystem.find_file(fs_path)

            if match:
                status = MappingStatus.MAPPED
                mapped += 1
                matched_ref_paths.add(match.normalized_path)
            else:
                status = MappingStatus.NOT_FOUND
                not_found += 1

            self.mappings.append(PathMapping(
                backup_file=bf,
                filesystem_path=fs_path,
                filesystem_file=match,
                status=status,
                notes="" if match else "Not found in reference filesystem"
            ))

        self.statistics.mapped_files = mapped
        self.statistics.not_found_files = not_found
        self.statistics.unmappable_files = 0  # All filesystem paths are inherently mappable
        self.statistics.backup_only_files = not_found

        # Count filesystem-only files
        fs_only = 0
        for rf in ref_files:
            if rf.normalized_path not in matched_ref_paths:
                fs_only += 1
        self.statistics.filesystem_only_files = fs_only

        # Coverage
        if self.statistics.total_filesystem_files > 0:
            self.statistics.backup_coverage_percent = (
                mapped / self.statistics.total_filesystem_files * 100
            )

        return self.mappings

    def get_mapping_for_backup_file(self, backup_file) -> Optional[PathMapping]:
        """Get the mapping for a specific backup file."""
        for mapping in self.mappings:
            if mapping.backup_file == backup_file:
                return mapping
        return None

    def get_mapping_for_filesystem_file(self, fs_file: FilesystemFile) -> Optional[PathMapping]:
        """Get the mapping for a specific filesystem file (reverse lookup)."""
        for mapping in self.mappings:
            if mapping.filesystem_file == fs_file:
                return mapping
        return None

    def get_mappings_by_domain(self) -> Dict[str, List[PathMapping]]:
        """Group mappings by domain."""
        by_domain: Dict[str, List[PathMapping]] = {}
        for mapping in self.mappings:
            domain = mapping.backup_file.domain
            if domain not in by_domain:
                by_domain[domain] = []
            by_domain[domain].append(mapping)
        return by_domain

    def get_unmapped_backup_files(self) -> list:
        """Get list of backup files that couldn't be mapped."""
        return [
            m.backup_file for m in self.mappings
            if m.status in (MappingStatus.NOT_FOUND, MappingStatus.UNMAPPABLE)
        ]

    def get_filesystem_files_not_in_backup(self) -> List[FilesystemFile]:
        """Get list of filesystem files that have no corresponding source file."""
        matched_paths = {
            m.filesystem_file.normalized_path
            for m in self.mappings
            if m.filesystem_file is not None
        }
        return [
            f for f in self.filesystem.files
            if not f.is_directory and f.normalized_path not in matched_paths
        ]
