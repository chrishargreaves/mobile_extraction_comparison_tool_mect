"""
Android Path Mapper Module

Maps Android backup domain/token paths to filesystem paths using:
1. AOSP-defined domain token mappings (r, f, db, sp, etc.)
2. APK suffix resolution from filesystem
3. Path equivalence handling (/data/data/ <-> /data/user/0/, etc.)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from android_backup_parser import (
    AndroidBackup, AndroidBackupFile,
    TOKEN_PATH_MAPPINGS, UNMAPPABLE_TOKENS,
)
from path_mapper import MappingStatus, PathMapping, MappingStatistics
from filesystem_loader import FilesystemAcquisition, FilesystemFile


class AndroidPathMapper:
    """Maps Android backup paths to filesystem paths."""

    def __init__(self, backup: AndroidBackup, filesystem: FilesystemAcquisition):
        """
        Initialize the mapper.

        Args:
            backup: Parsed Android backup
            filesystem: Loaded filesystem acquisition
        """
        self.backup = backup
        self.filesystem = filesystem
        self.mappings: List[PathMapping] = []
        self.statistics = MappingStatistics()
        self._apk_dir_cache: Dict[str, Optional[str]] = {}  # package -> resolved dir name

    def _resolve_apk_dir(self, package_name: str) -> Optional[str]:
        """
        Resolve the APK directory name with its random suffix.

        On the filesystem, APKs live at /data/app/<package>-<suffix>/
        where suffix is random. Scan the filesystem index to find it.

        Returns:
            The full directory name (e.g., "com.whatsapp-2") or None
        """
        if package_name in self._apk_dir_cache:
            return self._apk_dir_cache[package_name]

        prefix = f'/data/app/{package_name}-'
        for path in self.filesystem._file_index:
            if path.startswith(prefix):
                # Extract the directory name
                after_data_app = path[len('/data/app/'):]
                dir_name = after_data_app.split('/')[0]
                self._apk_dir_cache[package_name] = dir_name
                return dir_name

        self._apk_dir_cache[package_name] = None
        return None

    def _map_backup_file(self, backup_file: AndroidBackupFile) -> Tuple[Optional[str], str]:
        """
        Map a single backup file to its expected filesystem path.

        Returns:
            (filesystem_path, notes) where filesystem_path may be None for unmappable files
        """
        token = backup_file.token
        domain = backup_file.domain

        # Unmappable tokens (_manifest, k)
        if token in UNMAPPABLE_TOKENS:
            return None, f"Token '{token}' has no filesystem equivalent"

        # Shared storage: shared/N -> /data/media/N/
        if domain.startswith('shared/'):
            parts = domain.split('/', 1)
            user_id = parts[1] if len(parts) > 1 else '0'
            if backup_file.relative_path:
                return f'/data/media/{user_id}/{backup_file.relative_path}', "Shared storage"
            return f'/data/media/{user_id}', "Shared storage root"

        # No token means just "apps/<package>" entry itself
        if not token:
            return None, "Package root entry (no token)"

        # APK token needs suffix resolution
        if token == 'a':
            apk_dir = self._resolve_apk_dir(domain)
            if apk_dir:
                # Get the path after the 'a/' token
                path_parts = backup_file.relative_path.split('/', 1)
                remaining = path_parts[1] if len(path_parts) > 1 else ''
                if remaining:
                    fs_path = f'/data/app/{apk_dir}/{remaining}'
                else:
                    fs_path = f'/data/app/{apk_dir}'
                return fs_path, f"APK dir resolved: {apk_dir}"
            else:
                # Can't resolve suffix - try without suffix as fallback
                path_parts = backup_file.relative_path.split('/', 1)
                remaining = path_parts[1] if len(path_parts) > 1 else ''
                if remaining:
                    fs_path = f'/data/app/{domain}/{remaining}'
                else:
                    fs_path = f'/data/app/{domain}'
                return fs_path, f"APK dir suffix not found (using package name as fallback)"

        # Standard token mapping
        template = TOKEN_PATH_MAPPINGS.get(token)
        if template is None:
            return None, f"Unknown token: {token}"

        base_path = template.replace('{package}', domain).rstrip('/')

        # Get the path after the token
        path_parts = backup_file.relative_path.split('/', 1)
        remaining = path_parts[1] if len(path_parts) > 1 else ''

        if remaining:
            return f'{base_path}/{remaining}', f"Token '{token}' mapping"
        return base_path, f"Token '{token}' mapping"

    def map_all(self) -> List[PathMapping]:
        """
        Map all backup files to filesystem paths.

        Returns:
            List of PathMapping results
        """
        self.mappings = []
        self.statistics = MappingStatistics()
        mapped_fs_paths = set()
        backup_dir_paths = set()

        # Store total entry count
        self.statistics.manifest_db_row_count = self.backup.manifest_db_row_count

        # Count backup files and derive directory paths
        for bf in self.backup.files:
            if not bf.is_directory:
                self.statistics.total_backup_files += 1
                # Extract directory paths
                if bf.relative_path and '/' in bf.relative_path:
                    dir_path = bf.relative_path.rsplit('/', 1)[0]
                    parts = dir_path.split('/')
                    for i in range(len(parts)):
                        backup_dir_paths.add(f"{bf.domain}/{'/'.join(parts[:i+1])}")
                backup_dir_paths.add(bf.domain)

        self.statistics.total_backup_directories = len(backup_dir_paths)

        # Count filesystem files and directories
        for ff in self.filesystem.files:
            if ff.is_directory:
                self.statistics.total_filesystem_directories += 1
            else:
                self.statistics.total_filesystem_files += 1

        # Map each backup file
        for backup_file in self.backup.files:
            if backup_file.is_directory:
                continue

            fs_path, notes = self._map_backup_file(backup_file)

            if fs_path is None:
                mapping = PathMapping(
                    backup_file=backup_file,
                    filesystem_path=None,
                    filesystem_file=None,
                    status=MappingStatus.UNMAPPABLE,
                    notes=notes,
                )
                self.statistics.unmappable_files += 1
            else:
                fs_file = self.filesystem.find_file(fs_path)

                if fs_file:
                    mapping = PathMapping(
                        backup_file=backup_file,
                        filesystem_path=fs_path,
                        filesystem_file=fs_file,
                        status=MappingStatus.MAPPED,
                        notes=notes,
                    )
                    self.statistics.mapped_files += 1
                    mapped_fs_paths.add(fs_file.normalized_path)
                else:
                    mapping = PathMapping(
                        backup_file=backup_file,
                        filesystem_path=fs_path,
                        filesystem_file=None,
                        status=MappingStatus.NOT_FOUND,
                        notes=notes,
                    )
                    self.statistics.not_found_files += 1

            self.mappings.append(mapping)

        # Calculate files unique to each side
        self.statistics.backup_only_files = (
            self.statistics.not_found_files + self.statistics.unmappable_files
        )

        for ff in self.filesystem.files:
            if not ff.is_directory and ff.normalized_path not in mapped_fs_paths:
                self.statistics.filesystem_only_files += 1

        if self.statistics.total_filesystem_files > 0:
            self.statistics.backup_coverage_percent = (
                self.statistics.mapped_files / self.statistics.total_filesystem_files * 100
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
        """Group mappings by domain (package name)."""
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
        """Get list of filesystem files that have no corresponding backup file."""
        mapped_paths = {
            m.filesystem_file.normalized_path
            for m in self.mappings
            if m.filesystem_file is not None
        }

        return [
            f for f in self.filesystem.files
            if not f.is_directory and f.normalized_path not in mapped_paths
        ]
