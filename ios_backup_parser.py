"""
iOS Backup Parser Module

Handles parsing of encrypted and unencrypted iOS backups,
extracting file metadata and providing access to file contents.

Supports:
- Directory-based backups (standard iTunes backup folders)
- ZIP-archived backups
"""

import os
import io
import sqlite3
import plistlib
import zipfile
import tempfile
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union
from pathlib import Path


@dataclass
class ParsingLogEntry:
    """Single entry in the parsing log."""
    file_id: str
    domain: str
    relative_path: str
    status: str  # 'added_file', 'added_directory', 'skipped_no_content', 'error', 'size_mismatch'
    details: str = ""
    manifest_size: int = 0
    actual_size: Optional[int] = None


@dataclass
class ParsingLog:
    """Log of the manifest.db parsing process."""
    timestamp: str = ""
    total_rows: int = 0
    files_added: int = 0
    directories_added: int = 0
    skipped_no_content: int = 0
    errors: int = 0
    size_mismatches: int = 0  # Files where manifest size != actual size
    manifest_size_zero: int = 0  # Files with manifest size = 0 but actual content
    entries: List[ParsingLogEntry] = field(default_factory=list)
    _entry_by_file_id: Dict[str, ParsingLogEntry] = field(default_factory=dict)

    def add_entry(self, file_id: str, domain: str, relative_path: str, status: str,
                  details: str = "", manifest_size: int = 0):
        entry = ParsingLogEntry(file_id, domain, relative_path, status, details, manifest_size)
        self.entries.append(entry)
        if file_id:
            self._entry_by_file_id[file_id] = entry
        if status == 'added_file':
            self.files_added += 1
        elif status == 'added_directory':
            self.directories_added += 1
        elif status == 'skipped_no_content':
            self.skipped_no_content += 1
        elif status == 'error':
            self.errors += 1

    def update_actual_size(self, file_id: str, actual_size: Optional[int]):
        """Update an entry with the actual file size from disk."""
        if file_id in self._entry_by_file_id:
            entry = self._entry_by_file_id[file_id]
            entry.actual_size = actual_size
            if actual_size is not None:
                if entry.manifest_size != actual_size:
                    self.size_mismatches += 1
                    if entry.manifest_size == 0 and actual_size > 0:
                        self.manifest_size_zero += 1

    def to_text(self) -> str:
        """Generate a text report of the parsing log."""
        lines = [
            f"Manifest.db Parsing Log",
            f"======================",
            f"Timestamp: {self.timestamp}",
            f"",
            f"Summary:",
            f"  Total rows in manifest.db: {self.total_rows}",
            f"  Files added: {self.files_added}",
            f"  Directories added: {self.directories_added}",
            f"  Skipped (no content): {self.skipped_no_content}",
            f"  Errors: {self.errors}",
            f"",
            f"Size Verification:",
            f"  Files with size mismatch: {self.size_mismatches}",
            f"  Files with manifest size=0 but actual content: {self.manifest_size_zero}",
            f"",
            f"Details:",
            f"-" * 100,
        ]

        for entry in self.entries:
            path = f"{entry.domain}/{entry.relative_path}" if entry.relative_path else entry.domain
            line = f"[{entry.status:20}] {path}"
            if entry.details:
                line += f" ({entry.details})"

            # Add size mismatch flag
            if entry.actual_size is not None and entry.status == 'added_file':
                if entry.manifest_size != entry.actual_size:
                    line += f" ⚠️ SIZE MISMATCH: manifest={entry.manifest_size}, actual={entry.actual_size}"

            lines.append(line)

        return "\n".join(lines)


@dataclass
class BackupFile:
    """Represents a file from an iOS backup."""
    file_id: str  # The SHA1 hash filename in backup
    domain: str  # e.g., 'HomeDomain', 'AppDomain-com.example.app'
    relative_path: str  # Path within the domain
    file_size: int  # Size from manifest.db metadata (may be 0 or inaccurate)
    mode: int  # File mode (used to detect directories)
    modified_time: Optional[float] = None
    flags: int = 0  # Backup flags: 1=file, 2=directory
    actual_file_size: Optional[int] = None  # Actual size of backup file on disk

    @property
    def is_directory(self) -> bool:
        """Check if this entry is a directory."""
        # Check mode first (standard Unix directory mode)
        if (self.mode & 0o170000) == 0o040000:
            return True
        # Fallback: check flags (iOS backup specific: 2=directory)
        if self.flags == 2:
            return True
        # Fallback: if mode is 0, size is 0, and no file_id, likely a directory
        if self.mode == 0 and self.file_size == 0 and not self.file_id:
            return True
        return False

    @property
    def full_domain_path(self) -> str:
        """Get the full path including domain."""
        return f"{self.domain}/{self.relative_path}" if self.relative_path else self.domain


@dataclass
class iOSBackup:
    """Container for parsed iOS backup data."""
    path: str
    device_name: str = ""
    product_type: str = ""
    ios_version: str = ""
    serial_number: str = ""
    udid: str = ""
    is_encrypted: bool = False
    is_zipped: bool = False
    files: List[BackupFile] = field(default_factory=list)
    manifest_db_row_count: int = 0  # Number of rows in manifest.db Files table
    parsing_log: ParsingLog = field(default_factory=ParsingLog)  # Detailed parsing log
    _backup_handle: object = None  # iOSbackup library handle for encrypted backups
    _zip_handle: object = None  # ZipFile handle for zipped backups
    _password: Optional[str] = None

    def get_files_by_domain(self) -> Dict[str, List[BackupFile]]:
        """Group files by their domain."""
        by_domain: Dict[str, List[BackupFile]] = {}
        for f in self.files:
            if f.domain not in by_domain:
                by_domain[f.domain] = []
            by_domain[f.domain].append(f)
        return by_domain


class iOSBackupParser:
    """Parser for iOS backups (iTunes-style backups)."""

    def __init__(self, backup_path: str, password: Optional[str] = None):
        """
        Initialize the parser.

        Args:
            backup_path: Path to the iOS backup (directory or ZIP file)
            password: Optional password for encrypted backups
        """
        self.backup_path = backup_path
        self._password = password
        self._backup_lib_handle = None
        self._zip_file: Optional[zipfile.ZipFile] = None
        self._is_zipped = False
        self._manifest_db_row_count = 0  # Track rows in manifest.db
        self._parsing_log = ParsingLog()  # Detailed parsing log

    @staticmethod
    def is_ios_backup(path: str) -> bool:
        """
        Check if the given path is an iOS backup.

        Args:
            path: Path to check (directory or ZIP file)

        Returns:
            True if this looks like an iOS backup
        """
        # Check if it's a directory-based backup
        if os.path.isdir(path):
            manifest_db = os.path.join(path, 'Manifest.db')
            manifest_plist = os.path.join(path, 'Manifest.plist')
            return os.path.exists(manifest_db) and os.path.exists(manifest_plist)

        # Check if it's a zipped backup
        if os.path.isfile(path) and zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path, 'r') as zf:
                    namelist = zf.namelist()
                    # Check for Manifest.db and Manifest.plist at root or one level deep
                    has_manifest_db = any(
                        n == 'Manifest.db' or n.endswith('/Manifest.db')
                        for n in namelist
                    )
                    has_manifest_plist = any(
                        n == 'Manifest.plist' or n.endswith('/Manifest.plist')
                        for n in namelist
                    )
                    return has_manifest_db and has_manifest_plist
            except Exception:
                return False

        return False

    def _open_zip(self):
        """Open the ZIP file if needed."""
        if self._zip_file is None and zipfile.is_zipfile(self.backup_path):
            self._zip_file = zipfile.ZipFile(self.backup_path, 'r')
            self._is_zipped = True

    def _close_zip(self):
        """Close the ZIP file if open."""
        if self._zip_file is not None:
            self._zip_file.close()
            self._zip_file = None

    def _get_zip_prefix(self) -> str:
        """Get the prefix path inside the ZIP (if files are in a subdirectory)."""
        if not self._zip_file:
            return ""

        namelist = self._zip_file.namelist()

        # Check if Manifest.db is at root or in a subdirectory
        for name in namelist:
            if name == 'Manifest.db':
                return ""
            if name.endswith('/Manifest.db'):
                return name[:-len('Manifest.db')]

        return ""

    def _read_file_from_zip(self, filename: str) -> Optional[bytes]:
        """Read a file from the ZIP archive."""
        if not self._zip_file:
            return None

        prefix = self._get_zip_prefix()
        full_path = prefix + filename

        try:
            return self._zip_file.read(full_path)
        except KeyError:
            # Try without prefix
            try:
                return self._zip_file.read(filename)
            except KeyError:
                return None

    def _read_file(self, filename: str) -> Optional[bytes]:
        """Read a file from the backup (directory or ZIP)."""
        if self._is_zipped:
            return self._read_file_from_zip(filename)
        else:
            full_path = os.path.join(self.backup_path, filename)
            if os.path.exists(full_path):
                try:
                    with open(full_path, 'rb') as f:
                        return f.read()
                except Exception:
                    return None
        return None

    def _find_password(self) -> Optional[str]:
        """
        Look for password file in standard locations.

        Returns:
            Password string if found, None otherwise
        """
        if self._password:
            return self._password

        # Check for password.txt in backup directory/archive and parent
        if self._is_zipped:
            # Check inside ZIP
            content = self._read_file_from_zip('password.txt')
            if content:
                return content.decode('utf-8').strip()

            # Check next to ZIP file
            parent_dir = os.path.dirname(self.backup_path)
            password_file = os.path.join(parent_dir, 'password.txt')
            if os.path.exists(password_file):
                try:
                    with open(password_file, 'r', encoding='utf-8') as f:
                        return f.read().strip()
                except Exception:
                    pass
        else:
            # Check for password.txt in backup directory and parent
            locations = [
                os.path.join(self.backup_path, 'password.txt'),
                os.path.join(os.path.dirname(self.backup_path), 'password.txt'),
            ]

            for loc in locations:
                if os.path.exists(loc):
                    try:
                        with open(loc, 'r', encoding='utf-8') as f:
                            password = f.read().strip()
                            if password:
                                return password
                    except Exception:
                        pass

        return None

    def _is_encrypted(self) -> bool:
        """Check if the backup is encrypted."""
        content = self._read_file('Manifest.plist')
        if content:
            try:
                plist = plistlib.loads(content)
                return plist.get('IsEncrypted', False)
            except Exception:
                pass
        return False

    def _get_device_info(self) -> dict:
        """Extract device information from Info.plist."""
        content = self._read_file('Info.plist')
        if content:
            try:
                return plistlib.loads(content)
            except Exception:
                pass
        return {}

    def _parse_unencrypted(self) -> List[BackupFile]:
        """Parse an unencrypted backup from Manifest.db."""
        files = []

        # Initialize parsing log
        self._parsing_log = ParsingLog()
        self._parsing_log.timestamp = datetime.datetime.now().isoformat()

        # Read Manifest.db content
        db_content = self._read_file('Manifest.db')
        if not db_content:
            raise RuntimeError("Cannot read Manifest.db")

        # SQLite requires a file, so write to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp:
            tmp.write(db_content)
            tmp_path = tmp.name

        try:
            conn = sqlite3.connect(tmp_path)
            cursor = conn.cursor()

            # Query the Files table
            cursor.execute("""
                SELECT fileID, domain, relativePath, flags, file
                FROM Files
            """)

            rows = cursor.fetchall()
            self._manifest_db_row_count = len(rows)
            self._parsing_log.total_rows = len(rows)

            for row in rows:
                file_id, domain, relative_path, flags, file_blob = row

                # Parse file blob to get metadata
                file_size = 0
                mode = 0
                modified_time = None
                plist_keys = []

                if file_blob:
                    try:
                        # The file blob is a binary plist
                        file_info = plistlib.loads(file_blob)
                        file_size = file_info.get('Size', 0)
                        mode = file_info.get('Mode', 0)
                        modified_time = file_info.get('LastModified')
                        # Track keys for debugging size=0 cases
                        if isinstance(file_info, dict):
                            plist_keys = list(file_info.keys())
                    except Exception:
                        pass

                backup_file = BackupFile(
                    file_id=file_id or '',
                    domain=domain or '',
                    relative_path=relative_path or '',
                    file_size=file_size,
                    mode=mode,
                    modified_time=modified_time,
                    flags=flags or 0
                )

                # Determine status for logging
                if backup_file.is_directory:
                    status = 'added_directory'
                    details = f"flags={flags}, mode={oct(mode) if mode else 0}"
                else:
                    status = 'added_file'
                    details = f"size={file_size}, flags={flags}"
                    # For files with size=0, log the plist keys to help debug
                    if file_size == 0 and plist_keys:
                        details += f", plist_keys={plist_keys}"

                self._parsing_log.add_entry(
                    file_id=file_id or '',
                    domain=domain or '',
                    relative_path=relative_path or '',
                    status=status,
                    details=details,
                    manifest_size=file_size
                )

                files.append(backup_file)

            conn.close()

        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        return files

    def _parse_encrypted(self, password: str) -> Tuple[List[BackupFile], object]:
        """
        Parse an encrypted backup using iOSbackup library.

        Note: For zipped encrypted backups, we need to extract first
        since iOSbackup expects a directory.

        Returns:
            Tuple of (files list, backup handle for file access)
        """
        try:
            from iOSbackup import iOSbackup as iOSbackupLib
        except ImportError:
            raise ImportError(
                "iOSbackup library required for encrypted backups. "
                "Install with: pip install iOSbackup"
            )

        files = []

        if self._is_zipped:
            raise RuntimeError(
                "Encrypted backups in ZIP format are not yet supported. "
                "Please extract the ZIP file first."
            )

        # Get the backup UDID (directory name)
        udid = os.path.basename(self.backup_path)
        backup_root = os.path.dirname(self.backup_path)

        try:
            # Initialize iOSbackup with password
            backup = iOSbackupLib(
                udid=udid,
                cleartextpassword=password,
                derivedkey=None,
                backuproot=backup_root
            )

            # Get file list
            file_list = backup.getBackupFilesList()
            self._manifest_db_row_count = len(file_list)

            for file_info in file_list:
                # file_info format: (backupFile, domain, name, relativePath)
                if len(file_info) >= 4:
                    file_id, domain, name, relative_path = file_info[:4]

                    # Try to get additional metadata
                    file_size = 0
                    mode = 0

                    files.append(BackupFile(
                        file_id=file_id or '',
                        domain=domain or '',
                        relative_path=relative_path or '',
                        file_size=file_size,
                        mode=mode,
                        flags=0  # Not available from encrypted backup library
                    ))

            return files, backup

        except Exception as e:
            raise RuntimeError(f"Failed to parse encrypted backup: {e}")

    def _get_actual_file_sizes(self, files: List[BackupFile], progress_callback=None) -> None:
        """
        Read actual file sizes from backup files on disk.

        Args:
            files: List of BackupFile objects to update
            progress_callback: Optional callback(current, total, message) for progress updates
        """
        # Filter to non-directory files that have a file_id
        files_to_check = [f for f in files if not f.is_directory and f.file_id]
        total = len(files_to_check)

        if progress_callback:
            progress_callback(0, total, "Reading actual file sizes...")

        for i, bf in enumerate(files_to_check):
            if bf.file_id:
                # Backup files are stored as {file_id[:2]}/{file_id}
                if self._is_zipped and self._zip_file:
                    # For ZIP files, check the size in the archive
                    file_path = f"{bf.file_id[:2]}/{bf.file_id}"
                    try:
                        info = self._zip_file.getinfo(file_path)
                        bf.actual_file_size = info.file_size
                    except KeyError:
                        bf.actual_file_size = None
                else:
                    # For directory backups, check actual file on disk
                    file_path = os.path.join(self.backup_path, bf.file_id[:2], bf.file_id)
                    if os.path.exists(file_path):
                        bf.actual_file_size = os.path.getsize(file_path)
                    else:
                        bf.actual_file_size = None

                # Update parsing log with actual size
                self._parsing_log.update_actual_size(bf.file_id, bf.actual_file_size)

            if progress_callback and (i % 100 == 0 or i == total - 1):
                progress_callback(i + 1, total, f"Reading file sizes: {i + 1}/{total}")

    def parse(self, password_callback=None, progress_callback=None) -> iOSBackup:
        """
        Parse the iOS backup.

        Args:
            password_callback: Optional callback function that returns password
                              if backup is encrypted and no password was provided
            progress_callback: Optional callback(current, total, message) for progress updates

        Returns:
            iOSBackup object containing parsed backup data
        """
        if not self.is_ios_backup(self.backup_path):
            raise ValueError(f"Not a valid iOS backup: {self.backup_path}")

        # Open ZIP if needed
        if zipfile.is_zipfile(self.backup_path):
            self._open_zip()

        try:
            if progress_callback:
                progress_callback(0, 100, "Reading device info...")

            # Get device info
            device_info = self._get_device_info()

            is_encrypted = self._is_encrypted()
            files = []
            backup_handle = None
            password = None

            if progress_callback:
                progress_callback(10, 100, "Parsing manifest.db...")

            if is_encrypted:
                # Try to find password
                password = self._find_password()

                if not password and password_callback:
                    password = password_callback()

                if not password:
                    raise ValueError(
                        "Backup is encrypted but no password provided. "
                        "Place password.txt in the backup directory or provide password."
                    )

                files, backup_handle = self._parse_encrypted(password)
            else:
                files = self._parse_unencrypted()

            if progress_callback:
                progress_callback(50, 100, "Reading actual file sizes...")

            # Read actual file sizes from disk
            self._get_actual_file_sizes(files, progress_callback)

            if progress_callback:
                progress_callback(100, 100, "Backup parsing complete")

            return iOSBackup(
                path=self.backup_path,
                device_name=device_info.get('Device Name', ''),
                product_type=device_info.get('Product Type', ''),
                ios_version=device_info.get('Product Version', ''),
                serial_number=device_info.get('Serial Number', ''),
                udid=device_info.get('Unique Identifier', ''),
                is_encrypted=is_encrypted,
                is_zipped=self._is_zipped,
                files=files,
                manifest_db_row_count=self._manifest_db_row_count,
                parsing_log=self._parsing_log,
                _backup_handle=backup_handle,
                _zip_handle=self._zip_file if self._is_zipped else None,
                _password=password
            )

        except Exception:
            self._close_zip()
            raise

    def get_file_content(self, backup: iOSBackup, backup_file: BackupFile) -> Optional[bytes]:
        """
        Get the content of a file from the backup.

        Args:
            backup: Parsed backup object
            backup_file: The file to retrieve

        Returns:
            File contents as bytes, or None if unable to read
        """
        if backup_file.is_directory:
            return None

        if backup.is_encrypted and backup._backup_handle:
            # Use iOSbackup library for encrypted files
            try:
                # Need to use getFileDecryptedCopy or similar
                # This is a placeholder - actual implementation depends on iOSbackup API
                pass
            except Exception:
                return None

        # Construct file path within backup
        # File is stored as first two chars of hash / full hash
        file_path = f"{backup_file.file_id[:2]}/{backup_file.file_id}"

        if backup.is_zipped and backup._zip_handle:
            try:
                prefix = ""
                namelist = backup._zip_handle.namelist()
                # Find prefix
                for name in namelist:
                    if name.endswith('/Manifest.db'):
                        prefix = name[:-len('Manifest.db')]
                        break

                return backup._zip_handle.read(prefix + file_path)
            except KeyError:
                return None
        else:
            full_path = os.path.join(backup.path, file_path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, 'rb') as f:
                        return f.read()
                except Exception:
                    return None

        return None
