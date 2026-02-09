"""
Filesystem Acquisition Loader Module

Handles loading filesystem acquisitions from various formats:
- TAR archives (UFADE output)
- ZIP archives
- Extracted directories
"""

import os
import re
import sqlite3
import tarfile
import tempfile
import zipfile
import plistlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Iterator
from pathlib import Path


@dataclass
class FilesystemFile:
    """Represents a file from a filesystem acquisition."""
    path: str  # Full path in the filesystem
    size: int
    is_directory: bool
    modified_time: Optional[float] = None
    platform: str = 'ios'  # 'ios' or 'android'

    @property
    def normalized_path(self) -> str:
        """Get normalized path for consistent lookups."""
        path = self.path
        if self.platform == 'android':
            # Android: strip leading ./ and ensure leading /
            if path.startswith('./'):
                path = path[1:]
            if not path.startswith('/'):
                path = '/' + path
            return path
        else:
            # iOS: normalize to /private/... canonical form
            if path.startswith('/private/'):
                pass
            elif path.startswith('./private/'):
                path = path[1:]  # Remove leading .
            elif path.startswith('private/'):
                path = '/' + path
            elif path.startswith('./'):
                path = '/private' + path[1:]
            elif not path.startswith('/'):
                path = '/private/' + path
            return path


@dataclass
class FilesystemAcquisition:
    """Container for a filesystem acquisition."""
    path: str
    format: str  # 'tar', 'zip', or 'directory'
    platform: str = 'ios'  # 'ios' or 'android'
    files: List[FilesystemFile] = field(default_factory=list)
    _file_index: Dict[str, FilesystemFile] = field(default_factory=dict)
    _archive_handle: object = None

    # Container mappings by type: bundleID -> GUID (iOS only)
    app_container_mapping: Dict[str, str] = field(default_factory=dict)  # AppDomain
    group_container_mapping: Dict[str, str] = field(default_factory=dict)  # AppDomainGroup
    plugin_container_mapping: Dict[str, str] = field(default_factory=dict)  # AppDomainPlugin
    system_container_mapping: Dict[str, str] = field(default_factory=dict)  # SysContainerDomain
    system_group_mapping: Dict[str, str] = field(default_factory=dict)  # SysSharedContainerDomain

    # Legacy combined mapping for backwards compatibility
    container_mapping: Dict[str, str] = field(default_factory=dict)

    def build_index(self):
        """Build path index for fast lookups."""
        self._file_index = {}
        for f in self.files:
            np = f.normalized_path
            self._file_index[np] = f

            if self.platform == 'ios':
                # Also index by path without /private prefix
                if np.startswith('/private/'):
                    alt_path = np[8:]  # Remove '/private'
                    self._file_index[alt_path] = f
            elif self.platform == 'android':
                # Index Android path equivalences
                if np.startswith('/data/data/'):
                    alt = '/data/user/0/' + np[len('/data/data/'):]
                    self._file_index[alt] = f
                elif np.startswith('/data/user/0/'):
                    alt = '/data/data/' + np[len('/data/user/0/'):]
                    self._file_index[alt] = f

                if np.startswith('/data/media/0/'):
                    self._file_index['/storage/emulated/0/' + np[len('/data/media/0/'):]] = f
                    self._file_index['/sdcard/' + np[len('/data/media/0/'):]] = f
                elif np.startswith('/storage/emulated/0/'):
                    self._file_index['/data/media/0/' + np[len('/storage/emulated/0/'):]] = f
                    self._file_index['/sdcard/' + np[len('/storage/emulated/0/'):]] = f
                elif np.startswith('/sdcard/'):
                    self._file_index['/storage/emulated/0/' + np[len('/sdcard/'):]] = f
                    self._file_index['/data/media/0/' + np[len('/sdcard/'):]] = f

    def find_file(self, path: str) -> Optional[FilesystemFile]:
        """Find a file by path (handles various path formats)."""
        if not self._file_index:
            self.build_index()

        # Try direct lookup
        if path in self._file_index:
            return self._file_index[path]

        if self.platform == 'ios':
            # iOS: try with /private prefix
            normalized = path
            if normalized.startswith('/private/'):
                pass
            elif normalized.startswith('/'):
                normalized = '/private' + normalized
            else:
                normalized = '/private/' + normalized
            return self._file_index.get(normalized)
        else:
            # Android: ensure leading slash
            if not path.startswith('/'):
                return self._file_index.get('/' + path)
            return None

    def find_files_in_directory(self, dir_path: str) -> List[FilesystemFile]:
        """Find all files within a directory."""
        if not self._file_index:
            self.build_index()

        # Normalize directory path
        if not dir_path.endswith('/'):
            dir_path += '/'

        results = []
        for path, f in self._file_index.items():
            if path.startswith(dir_path):
                results.append(f)

        return results


class FilesystemLoader:
    """Loader for filesystem acquisitions in various formats."""

    CONTAINER_METADATA_FILENAME = '.com.apple.mobile_container_manager.metadata.plist'

    def __init__(self, path: str, progress_callback=None):
        """
        Initialize the loader.

        Args:
            path: Path to filesystem acquisition (TAR, ZIP, or directory)
            progress_callback: Optional callback function(current, total, message)
        """
        self.path = path
        self._format = self._detect_format()
        self._progress_callback = progress_callback

    def _report_progress(self, current: int, total: int, message: str = ""):
        """Report progress to callback if available."""
        if self._progress_callback:
            self._progress_callback(current, total, message)

    def _detect_format(self) -> str:
        """Detect the format of the filesystem acquisition."""
        if os.path.isdir(self.path):
            return 'directory'
        elif tarfile.is_tarfile(self.path):
            return 'tar'
        elif zipfile.is_zipfile(self.path):
            return 'zip'
        else:
            raise ValueError(f"Unknown filesystem acquisition format: {self.path}")

    def _load_from_tar(self) -> List[FilesystemFile]:
        """Load file list from TAR archive."""
        files = []

        try:
            # Get file size for progress estimation
            try:
                tar_size = os.path.getsize(self.path)
            except OSError:
                tar_size = 0

            self._report_progress(0, 100, f"Opening TAR archive ({tar_size // (1024*1024)} MB)...")

            with tarfile.open(self.path, 'r:*') as tar:
                # Stream members one at a time to avoid blocking on getmembers()
                count = 0
                while True:
                    member = tar.next()
                    if member is None:
                        break

                    files.append(FilesystemFile(
                        path='/' + member.name.lstrip('./'),
                        size=member.size,
                        is_directory=member.isdir(),
                        modified_time=member.mtime
                    ))
                    count += 1

                    if count % 1000 == 0:
                        if tar_size > 0:
                            # Estimate progress from file position
                            pos = tar.fileobj.tell() if hasattr(tar.fileobj, 'tell') else 0
                            self._report_progress(pos, tar_size,
                                f"Reading TAR: {count} entries ({pos * 100 // tar_size}%)")
                        else:
                            self._report_progress(count, count + 1000,
                                f"Reading TAR: {count} entries")

                self._report_progress(100, 100, f"Loaded {count} entries from TAR")
        except Exception as e:
            raise RuntimeError(f"Failed to load TAR archive: {e}")

        return files

    def _load_from_zip(self) -> List[FilesystemFile]:
        """Load file list from ZIP archive."""
        files = []

        try:
            with zipfile.ZipFile(self.path, 'r') as zf:
                members = zf.infolist()
                total = len(members)
                self._report_progress(0, total, f"Reading ZIP: 0/{total} entries")

                for i, info in enumerate(members):
                    # Convert date_time tuple to timestamp
                    import time
                    try:
                        mtime = time.mktime(info.date_time + (0, 0, -1))
                    except Exception:
                        mtime = None

                    files.append(FilesystemFile(
                        path='/' + info.filename.lstrip('./'),
                        size=info.file_size,
                        is_directory=info.is_dir(),
                        modified_time=mtime
                    ))

                    if i % 1000 == 0 and i > 0:
                        self._report_progress(i, total, f"Reading ZIP: {i}/{total} entries")

                self._report_progress(total, total, f"Loaded {total} entries from ZIP")
        except Exception as e:
            raise RuntimeError(f"Failed to load ZIP archive: {e}")

        return files

    def _load_from_directory(self) -> List[FilesystemFile]:
        """Load file list from extracted directory."""
        files = []
        base_len = len(self.path)
        count = 0

        self._report_progress(0, 100, "Scanning directory...")

        try:
            for root, dirs, filenames in os.walk(self.path):
                # Get relative path from base
                rel_root = root[base_len:]
                if not rel_root.startswith('/'):
                    rel_root = '/' + rel_root

                # Add directories
                for d in dirs:
                    dir_path = os.path.join(root, d)
                    rel_path = os.path.join(rel_root, d)
                    try:
                        stat = os.stat(dir_path)
                        files.append(FilesystemFile(
                            path=rel_path,
                            size=0,
                            is_directory=True,
                            modified_time=stat.st_mtime
                        ))
                    except Exception:
                        files.append(FilesystemFile(
                            path=rel_path,
                            size=0,
                            is_directory=True
                        ))

                # Add files
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.join(rel_root, filename)
                    try:
                        stat = os.stat(file_path)
                        files.append(FilesystemFile(
                            path=rel_path,
                            size=stat.st_size,
                            is_directory=False,
                            modified_time=stat.st_mtime
                        ))
                    except Exception:
                        files.append(FilesystemFile(
                            path=rel_path,
                            size=0,
                            is_directory=False
                        ))

                count += len(dirs) + len(filenames)
                if count % 1000 < len(dirs) + len(filenames):
                    self._report_progress(count, count + 1000,
                        f"Scanning directory: {count} entries")

            self._report_progress(100, 100, f"Loaded {len(files)} entries from directory")

        except Exception as e:
            raise RuntimeError(f"Failed to load directory: {e}")

        return files

    def _bulk_read_files(self, paths: set) -> dict:
        """
        Read multiple files from the acquisition in a single pass.

        For TAR archives this avoids re-opening the archive for each file.

        Args:
            paths: Set of file paths to read (as stored in FilesystemFile.path)

        Returns:
            Dict mapping path -> bytes content
        """
        results = {}
        # Build lookup set with normalized paths
        needed = {}
        for p in paths:
            clean = p.lstrip('/')
            needed[clean] = p
            needed['./' + clean] = p

        if self._format == 'tar':
            try:
                with tarfile.open(self.path, 'r:*') as tar:
                    while True:
                        member = tar.next()
                        if member is None:
                            break
                        name = member.name.lstrip('./')
                        orig_path = needed.get(name) or needed.get('./' + name)
                        if orig_path and not member.isdir():
                            try:
                                f = tar.extractfile(member)
                                if f:
                                    results[orig_path] = f.read()
                            except Exception:
                                pass
                            if len(results) == len(paths):
                                break  # Found everything, stop early
            except Exception:
                pass

        elif self._format == 'zip':
            try:
                with zipfile.ZipFile(self.path, 'r') as zf:
                    for info in zf.infolist():
                        name = info.filename.lstrip('./')
                        orig_path = needed.get(name) or needed.get('./' + name)
                        if orig_path:
                            try:
                                results[orig_path] = zf.read(info)
                            except Exception:
                                pass
            except Exception:
                pass

        elif self._format == 'directory':
            for p in paths:
                clean = p.lstrip('/')
                full_path = os.path.join(self.path, clean)
                if os.path.isfile(full_path):
                    try:
                        with open(full_path, 'rb') as fh:
                            results[p] = fh.read()
                    except Exception:
                        pass

        return results

    def _extract_container_mappings(self, acquisition: FilesystemAcquisition):
        """
        Extract app container bundle ID to GUID mappings from metadata plist files.

        Populates separate mappings for each container type:
        - app_container_mapping: /Containers/Data/Application/ -> AppDomain
        - group_container_mapping: /Containers/Shared/AppGroup/ -> AppDomainGroup
        - plugin_container_mapping: /Containers/Data/PluginKitPlugin/ -> AppDomainPlugin
        - system_container_mapping: /containers/Data/System/ -> SysContainerDomain
        - system_group_mapping: /containers/Shared/SystemGroup/ -> SysSharedContainerDomain

        Bundle containers (/containers/Bundle/Application/) are excluded as
        backups store files from Data containers, not Bundle containers.
        """
        # Container type patterns - each type maps to specific domains
        container_type_patterns = {
            'app': ['/Containers/Data/Application/', '/containers/Data/Application/'],
            'group': ['/Containers/Shared/AppGroup/', '/containers/Shared/AppGroup/'],
            'plugin': ['/Containers/Data/PluginKitPlugin/', '/containers/Data/PluginKitPlugin/'],
            'system': ['/containers/Data/System/', '/Containers/Data/System/'],
            'system_group': ['/containers/Shared/SystemGroup/', '/Containers/Shared/SystemGroup/'],
        }

        # Bundle container paths - skip these entirely
        bundle_container_patterns = [
            '/containers/Bundle/Application/',
            '/Containers/Bundle/Application/',
        ]

        def get_container_type(path: str) -> Optional[str]:
            """Determine which container type a path belongs to."""
            # First check if it's a bundle container (skip these)
            if any(pattern in path for pattern in bundle_container_patterns):
                return None
            # Check each container type
            for container_type, patterns in container_type_patterns.items():
                if any(pattern in path for pattern in patterns):
                    return container_type
            return None

        def extract_guid(path: str) -> Optional[str]:
            """Extract GUID from metadata plist path."""
            path_parts = path.split('/')
            for i, part in enumerate(path_parts):
                if part == self.CONTAINER_METADATA_FILENAME and i > 0:
                    guid = path_parts[i - 1]
                    # Validate it looks like a GUID
                    if len(guid) == 36 and guid.count('-') == 4:
                        return guid
            return None

        # First, collect all metadata plist files to process
        metadata_files = [
            f for f in acquisition.files
            if f.path.endswith(self.CONTAINER_METADATA_FILENAME)
            and get_container_type(f.path) is not None
        ]

        total = len(metadata_files)
        if total == 0:
            return

        # Bulk-read all metadata plists in a single pass through the archive
        self._report_progress(0, total, f"Reading {total} container metadata files...")
        paths_needed = {f.path for f in metadata_files}

        # Also include applicationState.db in the same pass
        appstate_file = None
        for f in acquisition.files:
            if f.path.endswith('FrontBoard/applicationState.db'):
                appstate_file = f
                paths_needed.add(f.path)
                break

        file_contents = self._bulk_read_files(paths_needed)
        self._report_progress(50, 100, f"Read {len(file_contents)} files, resolving containers...")

        # Store the appstate content for later use
        self._appstate_db_content = file_contents.get(appstate_file.path) if appstate_file else None

        # Process all metadata plist files
        for i, f in enumerate(metadata_files):
            self._report_progress(i, total, f"Resolving containers: {i}/{total}")

            container_type = get_container_type(f.path)

            try:
                content = file_contents.get(f.path)
                if not content:
                    continue

                plist = plistlib.loads(content)
                bundle_id = plist.get('MCMMetadataIdentifier')
                if not bundle_id:
                    continue

                guid = extract_guid(f.path)
                if not guid:
                    continue

                # Add to the appropriate mapping based on container type
                if container_type == 'app':
                    acquisition.app_container_mapping[bundle_id] = guid
                elif container_type == 'group':
                    acquisition.group_container_mapping[bundle_id] = guid
                elif container_type == 'plugin':
                    acquisition.plugin_container_mapping[bundle_id] = guid
                elif container_type == 'system':
                    acquisition.system_container_mapping[bundle_id] = guid
                elif container_type == 'system_group':
                    acquisition.system_group_mapping[bundle_id] = guid

                # Also add to legacy combined mapping (for backwards compatibility)
                acquisition.container_mapping[bundle_id] = guid

            except Exception:
                continue

        self._report_progress(total, total, f"Resolved {total} containers")

    def _extract_mappings_from_applicationstate_db(self, acquisition: FilesystemAcquisition):
        """
        Extract container mappings from applicationState.db as a fallback/supplement.

        The applicationState.db contains app state info including container paths.
        This is particularly useful when metadata plist files are missing.

        Location: /private/var/mobile/Library/FrontBoard/applicationState.db
        """
        # Use content already read during bulk pass if available
        db_content = getattr(self, '_appstate_db_content', None)

        if not db_content:
            # Fallback: find and read it individually
            db_file = None
            for f in acquisition.files:
                if f.path.endswith('FrontBoard/applicationState.db'):
                    db_file = f
                    break

            if not db_file:
                return

            db_content = self._read_file_content(db_file.path)
            if not db_content:
                return

        # Write to temp file for SQLite access
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp:
            tmp.write(db_content)
            tmp_path = tmp.name

        try:
            conn = sqlite3.connect(tmp_path)
            cursor = conn.cursor()

            # Query for app container info
            # The database has application_identifier_tab and kvs tables
            # kvs contains serialized data with container paths
            try:
                cursor.execute("""
                    SELECT application_identifier, value
                    FROM application_identifier_tab
                    JOIN kvs ON application_identifier_tab.id = kvs.application_identifier
                    WHERE kvs.key = 'compatibilityInfo'
                """)

                # GUID pattern
                guid_pattern = re.compile(r'([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})', re.IGNORECASE)

                for row in cursor.fetchall():
                    bundle_id, value_blob = row
                    if not bundle_id or not value_blob:
                        continue

                    try:
                        # The value is a binary plist
                        data = plistlib.loads(value_blob)

                        # Look for sandboxPath or containerPath in the data
                        sandbox_path = None
                        if isinstance(data, dict):
                            sandbox_path = data.get('sandboxPath') or data.get('containerPath')

                        if sandbox_path:
                            # Extract GUID from path like /var/mobile/Containers/Data/Application/GUID
                            match = guid_pattern.search(sandbox_path)
                            if match:
                                guid = match.group(1).upper()

                                # Determine container type from path
                                if '/Containers/Data/Application/' in sandbox_path:
                                    if bundle_id not in acquisition.app_container_mapping:
                                        acquisition.app_container_mapping[bundle_id] = guid
                                elif '/Containers/Shared/AppGroup/' in sandbox_path:
                                    if bundle_id not in acquisition.group_container_mapping:
                                        acquisition.group_container_mapping[bundle_id] = guid
                                elif '/Containers/Data/PluginKitPlugin/' in sandbox_path:
                                    if bundle_id not in acquisition.plugin_container_mapping:
                                        acquisition.plugin_container_mapping[bundle_id] = guid

                                # Also update legacy mapping if not present
                                if bundle_id not in acquisition.container_mapping:
                                    acquisition.container_mapping[bundle_id] = guid

                    except Exception:
                        continue

            except sqlite3.Error:
                pass

            conn.close()

        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _read_file_content(self, file_path: str) -> Optional[bytes]:
        """Read content of a file from the acquisition."""
        # Normalize path
        clean_path = file_path.lstrip('/')

        if self._format == 'tar':
            try:
                with tarfile.open(self.path, 'r:*') as tar:
                    # Try various path formats
                    for try_path in [clean_path, './' + clean_path, file_path]:
                        try:
                            member = tar.getmember(try_path)
                            f = tar.extractfile(member)
                            if f:
                                return f.read()
                        except KeyError:
                            continue
            except Exception:
                pass

        elif self._format == 'zip':
            try:
                with zipfile.ZipFile(self.path, 'r') as zf:
                    # Try various path formats
                    for try_path in [clean_path, './' + clean_path, file_path.lstrip('/')]:
                        try:
                            return zf.read(try_path)
                        except KeyError:
                            continue
            except Exception:
                pass

        elif self._format == 'directory':
            full_path = os.path.join(self.path, clean_path)
            if os.path.isfile(full_path):
                try:
                    with open(full_path, 'rb') as f:
                        return f.read()
                except Exception:
                    pass

        return None

    def _detect_platform(self, files: List[FilesystemFile]) -> str:
        """Detect whether filesystem is iOS or Android based on characteristic paths."""
        for f in files[:5000]:
            path_lower = f.path.lower()
            # Android indicators
            if '/data/data/' in path_lower or '/data/app/' in path_lower:
                return 'android'
            if '/system/app/' in path_lower or '/system/framework/' in path_lower:
                return 'android'
            # iOS indicators
            if '/private/var/mobile/' in path_lower or '/containers/data/application/' in path_lower:
                return 'ios'
        return 'ios'  # Default to iOS

    def load(self) -> FilesystemAcquisition:
        """
        Load the filesystem acquisition.

        Returns:
            FilesystemAcquisition object containing all file metadata
        """
        if self._format == 'tar':
            files = self._load_from_tar()
        elif self._format == 'zip':
            files = self._load_from_zip()
        else:
            files = self._load_from_directory()

        # Auto-detect platform
        platform = self._detect_platform(files)

        # Set platform on all files
        for f in files:
            f.platform = platform

        acquisition = FilesystemAcquisition(
            path=self.path,
            format=self._format,
            platform=platform,
            files=files,
        )

        # Build index for fast lookups
        self._report_progress(0, 100, "Building file index...")
        acquisition.build_index()

        # Extract container mappings only for iOS filesystems
        if platform == 'ios':
            self._report_progress(0, 100, "Extracting container mappings...")
            self._extract_container_mappings(acquisition)

            self._report_progress(0, 100, "Reading application state database...")
            self._extract_mappings_from_applicationstate_db(acquisition)

        return acquisition

    def get_file_content(self, acquisition: FilesystemAcquisition, fs_file: FilesystemFile) -> Optional[bytes]:
        """
        Get the content of a file from the acquisition.

        Args:
            acquisition: Loaded acquisition object
            fs_file: The file to retrieve

        Returns:
            File contents as bytes, or None if unable to read
        """
        if fs_file.is_directory:
            return None

        return self._read_file_content(fs_file.path)
