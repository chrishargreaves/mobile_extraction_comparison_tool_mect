"""
Path Mapper Module

Maps iOS backup domain paths to filesystem paths using:
1. UFADE-style domain mappings
2. Container metadata plist resolution for app-specific paths
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

from ios_backup_parser import BackupFile, iOSBackup
from filesystem_loader import FilesystemAcquisition, FilesystemFile


class MappingStatus(Enum):
    """Status of a path mapping."""
    MAPPED = "mapped"  # Successfully mapped to filesystem path
    NOT_FOUND = "not_found"  # Mapped but file not found in filesystem
    UNMAPPABLE = "unmappable"  # Domain cannot be mapped (e.g., unknown domain)
    DIRECTORY = "directory"  # Is a directory, not a file


@dataclass
class PathMapping:
    """Result of mapping a backup file to filesystem location."""
    backup_file: BackupFile
    filesystem_path: Optional[str]  # Expected path in filesystem
    filesystem_file: Optional[FilesystemFile]  # Matched file if found
    status: MappingStatus
    notes: str = ""


@dataclass
class MappingStatistics:
    """Statistics about the mapping process."""
    total_backup_files: int = 0
    total_backup_directories: int = 0  # Unique directory paths implied by backup files
    total_filesystem_files: int = 0
    total_filesystem_directories: int = 0
    mapped_files: int = 0
    not_found_files: int = 0
    unmappable_files: int = 0
    backup_only_files: int = 0  # Files in backup but not filesystem
    filesystem_only_files: int = 0  # Files in filesystem but not backup
    backup_coverage_percent: float = 0.0  # What % of filesystem does backup represent
    manifest_db_row_count: int = 0  # Raw row count from manifest.db


class PathMapper:
    """Maps iOS backup paths to filesystem paths."""

    # Domain to filesystem path mapping (from UFADE)
    DOMAIN_MAPPINGS = {
        'KeychainDomain': '/private/var/Keychains',
        'CameraRollDomain': '/private/var/mobile',
        'MobileDeviceDomain': '/private/var/MobileDevice',
        'WirelessDomain': '/private/var/wireless',
        'InstallDomain': '/private/var/installd',
        'KeyboardDomain': '/private/var/mobile',
        'HomeDomain': '/private/var/mobile',
        'SystemPreferencesDomain': '/private/var/preferences',
        'DatabaseDomain': '/private/var/db',
        'TonesDomain': '/private/var/mobile',
        'RootDomain': '/private/var/root',
        'BooksDomain': '/private/var/mobile/Media/Books',
        'ManagedPreferencesDomain': '/private/var/Managed Preferences',
        'HomeKitDomain': '/private/var/mobile',
        'MediaDomain': '/private/var/mobile',
        'HealthDomain': '/private/var/mobile/Library',
        'ProtectedDomain': '/private/var/protected',
        'NetworkDomain': '/private/var/networkd',
        # Container domains are handled separately
        'AppDomain': '/private/var/mobile/Containers/Data/Application',
        'AppDomainGroup': '/private/var/mobile/Containers/Shared/AppGroup',
        'AppDomainPlugin': '/private/var/mobile/Containers/Data/PluginKitPlugin',
        'SysContainerDomain': '/private/var/containers/Data/System',
        'SysSharedContainerDomain': '/private/var/containers/Shared/SystemGroup',
        # Magnet Acquire Quick Image: AFC-captured files from /private/var/mobile/Media/
        'Filesystem': '/private/var/mobile/Media',
    }

    def __init__(self, backup: iOSBackup, filesystem: FilesystemAcquisition):
        """
        Initialize the mapper.

        Args:
            backup: Parsed iOS backup
            filesystem: Loaded filesystem acquisition
        """
        self.backup = backup
        self.filesystem = filesystem
        self.mappings: List[PathMapping] = []
        self.statistics = MappingStatistics()

    def _parse_domain(self, domain: str) -> Tuple[str, Optional[str]]:
        """
        Parse a domain string into base domain and identifier.

        Args:
            domain: Domain string like 'HomeDomain' or 'AppDomain-com.example.app'

        Returns:
            Tuple of (base_domain, identifier) where identifier may be None
        """
        if '-' in domain:
            parts = domain.split('-', 1)
            return parts[0], parts[1]
        return domain, None

    def _get_container_guid(self, bundle_id: str, domain_type: str) -> Optional[str]:
        """
        Get the container GUID for a bundle ID from the correct container type mapping.

        Args:
            bundle_id: App bundle identifier
            domain_type: One of 'app', 'group', 'plugin', 'system', 'system_group'

        Returns:
            GUID string if found, None otherwise
        """
        if domain_type == 'app':
            return self.filesystem.app_container_mapping.get(bundle_id)
        elif domain_type == 'group':
            return self.filesystem.group_container_mapping.get(bundle_id)
        elif domain_type == 'plugin':
            return self.filesystem.plugin_container_mapping.get(bundle_id)
        elif domain_type == 'system':
            return self.filesystem.system_container_mapping.get(bundle_id)
        elif domain_type == 'system_group':
            return self.filesystem.system_group_mapping.get(bundle_id)
        return None

    def _map_domain_path(self, backup_file: BackupFile) -> Tuple[Optional[str], str]:
        """
        Map a backup file's domain path to filesystem path.

        Args:
            backup_file: The backup file to map

        Returns:
            Tuple of (filesystem_path, notes) where filesystem_path may be None
        """
        base_domain, identifier = self._parse_domain(backup_file.domain)

        # Handle app-specific domains
        if base_domain in ('AppDomain', 'AppDomainGroup', 'AppDomainPlugin'):
            if identifier:
                # Determine which container type to look up
                if base_domain == 'AppDomain':
                    domain_type = 'app'
                elif base_domain == 'AppDomainGroup':
                    domain_type = 'group'
                else:  # AppDomainPlugin
                    domain_type = 'plugin'

                # Try to resolve via container mapping for the correct type
                guid = self._get_container_guid(identifier, domain_type)

                if guid:
                    if base_domain == 'AppDomain':
                        base_path = f'/private/var/mobile/Containers/Data/Application/{guid}'
                    elif base_domain == 'AppDomainGroup':
                        base_path = f'/private/var/mobile/Containers/Shared/AppGroup/{guid}'
                    else:  # AppDomainPlugin
                        base_path = f'/private/var/mobile/Containers/Data/PluginKitPlugin/{guid}'

                    if backup_file.relative_path:
                        return f'{base_path}/{backup_file.relative_path}', f"Resolved via container mapping: {identifier} -> {guid}"
                    return base_path, f"Resolved via container mapping: {identifier} -> {guid}"

                # Fallback: use bundle ID directly (may not match)
                if base_domain == 'AppDomain':
                    base_path = f'/private/var/mobile/Containers/Data/Application/{identifier}'
                elif base_domain == 'AppDomainGroup':
                    base_path = f'/private/var/mobile/Containers/Shared/AppGroup/{identifier}'
                else:
                    base_path = f'/private/var/mobile/Containers/Data/PluginKitPlugin/{identifier}'

                if backup_file.relative_path:
                    return f'{base_path}/{backup_file.relative_path}', f"Using bundle ID as fallback (GUID not found): {identifier}"
                return base_path, f"Using bundle ID as fallback (GUID not found): {identifier}"

        # Handle system container domains
        if base_domain in ('SysContainerDomain', 'SysSharedContainerDomain'):
            if identifier:
                # Determine which container type to look up
                if base_domain == 'SysContainerDomain':
                    domain_type = 'system'
                else:
                    domain_type = 'system_group'

                # Try to resolve via container mapping
                guid = self._get_container_guid(identifier, domain_type)

                if guid:
                    if base_domain == 'SysContainerDomain':
                        base_path = f'/private/var/containers/Data/System/{guid}'
                    else:
                        base_path = f'/private/var/containers/Shared/SystemGroup/{guid}'

                    if backup_file.relative_path:
                        return f'{base_path}/{backup_file.relative_path}', f"Resolved via container mapping: {identifier} -> {guid}"
                    return base_path, f"Resolved via container mapping: {identifier} -> {guid}"

                # Fallback: use identifier directly (may not match)
                if base_domain == 'SysContainerDomain':
                    base_path = f'/private/var/containers/Data/System/{identifier}'
                else:
                    base_path = f'/private/var/containers/Shared/SystemGroup/{identifier}'

                if backup_file.relative_path:
                    return f'{base_path}/{backup_file.relative_path}', f"Using identifier as fallback (GUID not found): {identifier}"
                return base_path, f"Using identifier as fallback (GUID not found): {identifier}"

        # Standard domain mapping
        if base_domain in self.DOMAIN_MAPPINGS:
            base_path = self.DOMAIN_MAPPINGS[base_domain]
            if backup_file.relative_path:
                return f'{base_path}/{backup_file.relative_path}', ""
            return base_path, ""

        # Unknown domain
        return None, f"Unknown domain: {backup_file.domain}"

    def map_all(self) -> List[PathMapping]:
        """
        Map all backup files to filesystem paths.

        Returns:
            List of PathMapping results
        """
        self.mappings = []
        mapped_fs_paths = set()
        backup_dir_paths = set()  # Track unique directory paths in backup

        # Store manifest.db row count from backup
        self.statistics.manifest_db_row_count = self.backup.manifest_db_row_count

        # Count backup files and derive directory paths
        for bf in self.backup.files:
            if not bf.is_directory:
                self.statistics.total_backup_files += 1
                # Extract directory path from file's relative path
                if bf.relative_path and '/' in bf.relative_path:
                    dir_path = bf.relative_path.rsplit('/', 1)[0]
                    # Add all parent directories
                    parts = dir_path.split('/')
                    for i in range(len(parts)):
                        backup_dir_paths.add(f"{bf.domain}/{'/'.join(parts[:i+1])}")
                # Also count domain as a directory
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
            # Skip directories for mapping
            if backup_file.is_directory:
                continue

            # Map the path
            fs_path, notes = self._map_domain_path(backup_file)

            if fs_path is None:
                # Unmappable
                mapping = PathMapping(
                    backup_file=backup_file,
                    filesystem_path=None,
                    filesystem_file=None,
                    status=MappingStatus.UNMAPPABLE,
                    notes=notes
                )
                self.statistics.unmappable_files += 1
            else:
                # Try to find in filesystem
                fs_file = self.filesystem.find_file(fs_path)

                if fs_file:
                    mapping = PathMapping(
                        backup_file=backup_file,
                        filesystem_path=fs_path,
                        filesystem_file=fs_file,
                        status=MappingStatus.MAPPED,
                        notes=notes
                    )
                    self.statistics.mapped_files += 1
                    mapped_fs_paths.add(fs_file.normalized_path)
                else:
                    mapping = PathMapping(
                        backup_file=backup_file,
                        filesystem_path=fs_path,
                        filesystem_file=None,
                        status=MappingStatus.NOT_FOUND,
                        notes=notes
                    )
                    self.statistics.not_found_files += 1

            self.mappings.append(mapping)

        # Calculate files unique to each side
        self.statistics.backup_only_files = (
            self.statistics.not_found_files + self.statistics.unmappable_files
        )

        # Count filesystem files not in backup
        for ff in self.filesystem.files:
            if not ff.is_directory and ff.normalized_path not in mapped_fs_paths:
                self.statistics.filesystem_only_files += 1

        # Calculate backup coverage of filesystem
        if self.statistics.total_filesystem_files > 0:
            self.statistics.backup_coverage_percent = (
                self.statistics.mapped_files / self.statistics.total_filesystem_files * 100
            )

        return self.mappings

    def get_mapping_for_backup_file(self, backup_file: BackupFile) -> Optional[PathMapping]:
        """
        Get the mapping for a specific backup file.

        Args:
            backup_file: The backup file to look up

        Returns:
            PathMapping if found, None otherwise
        """
        for mapping in self.mappings:
            if mapping.backup_file == backup_file:
                return mapping
        return None

    def get_mapping_for_filesystem_file(self, fs_file: FilesystemFile) -> Optional[PathMapping]:
        """
        Get the mapping for a specific filesystem file (reverse lookup).

        Args:
            fs_file: The filesystem file to look up

        Returns:
            PathMapping if found, None otherwise
        """
        for mapping in self.mappings:
            if mapping.filesystem_file == fs_file:
                return mapping
        return None

    def get_mappings_by_domain(self) -> Dict[str, List[PathMapping]]:
        """
        Group mappings by domain.

        Returns:
            Dictionary mapping domain names to lists of PathMappings
        """
        by_domain: Dict[str, List[PathMapping]] = {}
        for mapping in self.mappings:
            base_domain, _ = self._parse_domain(mapping.backup_file.domain)
            if base_domain not in by_domain:
                by_domain[base_domain] = []
            by_domain[base_domain].append(mapping)
        return by_domain

    def get_unmapped_backup_files(self) -> List[BackupFile]:
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
