"""
Android Backup Parser Module

Handles parsing of encrypted and unencrypted Android .ab backup files,
extracting file metadata and providing access to file contents.

Android backup file format:
- Line 1: "ANDROID BACKUP" (magic string)
- Line 2: Format version (integer)
- Line 3: Compression flag (1 = compressed with zlib)
- Line 4: Encryption type ("none" or "AES-256")
- For encrypted: 5 additional lines with crypto parameters
- Payload: optionally AES-256 encrypted, optionally zlib-compressed tar data

Internal tar structure:
- apps/<package>/<token>/<path>  (per-app data)
- shared/<N>/<path>              (shared storage)

Supports:
- Unencrypted backups
- AES-256 encrypted backups (requires pyaes library)
- Password lookup from password.txt or interactive callback
"""

import os
import io
import tarfile
import zlib
import hashlib
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ios_backup_parser import ParsingLog, ParsingLogEntry


# Tokens that have no filesystem equivalent
UNMAPPABLE_TOKENS = {'_manifest', 'k'}

# AOSP domain token to filesystem path template mappings
# {package} is replaced with the actual package name
TOKEN_PATH_MAPPINGS = {
    'a':    '/data/app/{package}/',
    'r':    '/data/data/{package}/',
    'f':    '/data/data/{package}/files/',
    'db':   '/data/data/{package}/databases/',
    'sp':   '/data/data/{package}/shared_prefs/',
    'c':    '/data/data/{package}/cache/',
    'nb':   '/data/data/{package}/no_backup/',
    'ef':   '/storage/emulated/0/Android/data/{package}/files/',
    'obb':  '/storage/emulated/0/Android/obb/{package}/',
    'd_r':  '/data/user_de/0/{package}/',
    'd_f':  '/data/user_de/0/{package}/files/',
    'd_db': '/data/user_de/0/{package}/databases/',
    'd_sp': '/data/user_de/0/{package}/shared_prefs/',
    'd_c':  '/data/user_de/0/{package}/cache/',
    'd_nb': '/data/user_de/0/{package}/no_backup/',
}

# All known tokens (for parsing)
KNOWN_TOKENS = set(TOKEN_PATH_MAPPINGS.keys()) | UNMAPPABLE_TOKENS


class AndroidBackupError(Exception):
    """Exception raised for Android backup parsing errors."""
    pass


@dataclass
class AndroidBackupFile:
    """Represents a file from an Android backup.

    Duck-type compatible with BackupFile from ios_backup_parser.
    """
    file_id: str  # Tar member name (e.g., "apps/com.whatsapp/db/msgstore.db")
    domain: str  # Package name (e.g., "com.whatsapp") or "shared/0"
    relative_path: str  # Token + remaining path (e.g., "db/msgstore.db")
    file_size: int
    mode: int
    modified_time: Optional[float] = None
    flags: int = 0
    actual_file_size: Optional[int] = None
    token: str = ""  # The AOSP backup token (r, f, db, sp, a, k, _manifest, etc.)

    @property
    def is_directory(self) -> bool:
        """Check if this entry is a directory."""
        if (self.mode & 0o170000) == 0o040000:
            return True
        return False

    @property
    def full_domain_path(self) -> str:
        """Get the full path including domain."""
        return f"{self.domain}/{self.relative_path}" if self.relative_path else self.domain


@dataclass
class AndroidBackup:
    """Container for parsed Android backup data.

    Duck-type compatible with iOSBackup from ios_backup_parser.
    """
    path: str
    device_name: str = "Android Device"
    product_type: str = ""
    ios_version: str = ""  # Empty for Android; kept for duck-typing
    android_version: str = ""
    serial_number: str = ""
    udid: str = ""
    is_encrypted: bool = False
    is_zipped: bool = False  # Always False for .ab files
    files: List[AndroidBackupFile] = field(default_factory=list)
    manifest_db_row_count: int = 0  # Total tar entries (for statistics display)
    parsing_log: ParsingLog = field(default_factory=ParsingLog)
    format_version: int = 0
    backup_type: str = "android"
    _backup_handle: object = None  # tarfile handle for content extraction
    _tar_data: object = None  # Raw decompressed tar bytes (keeps BytesIO alive)
    _member_lookup: Dict = field(default_factory=dict)  # member name -> TarInfo
    _zip_handle: object = None  # Always None
    _password: Optional[str] = None

    def get_files_by_domain(self) -> Dict[str, List[AndroidBackupFile]]:
        """Group files by their domain (package name)."""
        by_domain: Dict[str, List[AndroidBackupFile]] = {}
        for f in self.files:
            if f.domain not in by_domain:
                by_domain[f.domain] = []
            by_domain[f.domain].append(f)
        return by_domain


class AndroidBackupParser:
    """Parser for Android .ab backup files."""

    PBKDF2_KEY_SIZE = 32

    def __init__(self, backup_path: str, password: Optional[str] = None):
        """
        Initialize the parser.

        Args:
            backup_path: Path to the .ab backup file
            password: Optional password for encrypted backups
        """
        self.backup_path = os.path.abspath(backup_path)
        self._password = password

    @staticmethod
    def is_android_backup(path: str) -> bool:
        """Check if a given path is an Android backup file."""
        if not os.path.isfile(path):
            return False

        try:
            with open(path, 'rb') as f:
                magic = f.readline()
                return magic == b'ANDROID BACKUP\n'
        except (IOError, OSError):
            return False

    def _find_password(self) -> Optional[str]:
        """Look for password.txt in backup directory or parent."""
        backup_dir = os.path.dirname(self.backup_path)

        # Check same directory as .ab file
        password_file = os.path.join(backup_dir, 'password.txt')
        if os.path.exists(password_file):
            return self._read_password_file(password_file)

        # Check parent directory
        parent_dir = os.path.dirname(backup_dir)
        password_file = os.path.join(parent_dir, 'password.txt')
        if os.path.exists(password_file):
            return self._read_password_file(password_file)

        return None

    def _read_password_file(self, path: str) -> Optional[str]:
        """Read password from file."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return None

    def _parse_header(self, f) -> dict:
        """Parse the .ab file header."""
        header = {}

        # Line 1: Magic (already validated by is_android_backup)
        magic = f.readline()
        if magic != b'ANDROID BACKUP\n':
            raise AndroidBackupError(f"Invalid Android backup: expected 'ANDROID BACKUP', got {magic!r}")

        # Line 2: Format version
        header['format_version'] = int(f.readline().strip())

        # Line 3: Compression flag
        header['compression'] = int(f.readline().strip())

        # Line 4: Encryption type
        header['encryption'] = f.readline().decode('utf-8').strip()

        if header['encryption'] == 'AES-256':
            # 5 additional lines for encrypted backups
            header['user_salt'] = bytes.fromhex(f.readline().decode('utf-8').strip())
            header['checksum_salt'] = bytes.fromhex(f.readline().decode('utf-8').strip())
            header['pbkdf2_rounds'] = int(f.readline().strip())
            header['user_iv'] = bytes.fromhex(f.readline().decode('utf-8').strip())
            header['master_key_blob'] = bytes.fromhex(f.readline().decode('utf-8').strip())

        return header

    def _decrypt_payload(self, encrypted_data: bytes, header: dict, password: str) -> bytes:
        """Decrypt an AES-256 encrypted backup payload."""
        try:
            import pyaes
        except ImportError:
            raise AndroidBackupError(
                "pyaes library required for encrypted Android backups. "
                "Install with: pip install pyaes"
            )

        # Generate user key from password using PBKDF2
        user_key = hashlib.pbkdf2_hmac(
            'sha1',
            password.encode('utf-8'),
            header['user_salt'],
            header['pbkdf2_rounds'],
            self.PBKDF2_KEY_SIZE
        )

        # Decrypt the master key blob
        aes = pyaes.AESModeOfOperationCBC(user_key, header['user_iv'])
        decrypted_blob = b''
        offset = 0
        while offset < len(header['master_key_blob']):
            decrypted_blob += aes.decrypt(header['master_key_blob'][offset:offset + 16])
            offset += 16

        # Parse the decrypted master key blob
        blob = io.BytesIO(decrypted_blob)
        master_iv_length = blob.read(1)[0]
        master_iv = blob.read(master_iv_length)
        master_key_length = blob.read(1)[0]
        master_key = blob.read(master_key_length)
        master_checksum_length = blob.read(1)[0]
        master_checksum = blob.read(master_checksum_length)

        # Verify checksum
        if header['format_version'] >= 2:
            converted_key = self._convert_to_utf8_bytes(master_key)
        else:
            converted_key = master_key

        expected_checksum = hashlib.pbkdf2_hmac(
            'sha1',
            converted_key,
            header['checksum_salt'],
            header['pbkdf2_rounds'],
            self.PBKDF2_KEY_SIZE
        )

        if master_checksum != expected_checksum:
            raise AndroidBackupError("Invalid password or corrupted backup")

        # Decrypt the payload
        decrypter = pyaes.Decrypter(pyaes.AESModeOfOperationCBC(master_key, master_iv))
        decrypted_data = decrypter.feed(encrypted_data) + decrypter.feed()

        return decrypted_data

    @staticmethod
    def _convert_to_utf8_bytes(input_bytes: bytes) -> bytes:
        """Convert bytes to UTF-8 byte array format (for checksum verification in v2+ backups)."""
        output = []
        for byte in input_bytes:
            if byte < 0x80:
                output.append(byte)
            else:
                output.append(0xef | (byte >> 12))
                output.append(0xbc | ((byte >> 6) & 0x3f))
                output.append(0x80 | (byte & 0x3f))
        return bytes(output)

    @staticmethod
    def _parse_tar_path(member_name: str) -> Tuple[str, str, str]:
        """Parse a tar member name into (domain, token, relative_path).

        Returns:
            (domain, token, relative_path) where:
            - domain: package name or "shared/N"
            - token: AOSP backup token or ""
            - relative_path: token + remaining path for tree display
        """
        parts = member_name.strip('./').split('/')

        if parts[0] == 'apps' and len(parts) >= 2:
            package_name = parts[1]
            if len(parts) >= 3:
                # Check if parts[2] is a known token
                potential_token = parts[2]
                # Handle device-encrypted tokens that contain underscores (d_r, d_f, etc.)
                if potential_token in KNOWN_TOKENS:
                    token = potential_token
                    relative_path = '/'.join(parts[2:])
                    return package_name, token, relative_path
                else:
                    # Unknown token - treat the whole remaining path as relative
                    relative_path = '/'.join(parts[2:])
                    return package_name, potential_token, relative_path
            else:
                # Just "apps/<package>" with no further path
                return package_name, '', ''

        elif parts[0] == 'shared' and len(parts) >= 2:
            # shared/<N>/<path>
            domain = f"shared/{parts[1]}"
            relative_path = '/'.join(parts[2:]) if len(parts) > 2 else ''
            return domain, '', relative_path

        else:
            # Unknown top-level structure
            return parts[0], '', '/'.join(parts[1:]) if len(parts) > 1 else ''

    def parse(self, password_callback=None, progress_callback=None) -> AndroidBackup:
        """
        Parse the Android backup file.

        Args:
            password_callback: Callable that returns a password string (for encrypted backups)
            progress_callback: Callable(current, total, message) for progress reporting

        Returns:
            AndroidBackup object with parsed data
        """
        if progress_callback:
            progress_callback(0, 100, "Reading Android backup header...")

        # Parse header
        with open(self.backup_path, 'rb') as f:
            header = self._parse_header(f)

            is_encrypted = header['encryption'] == 'AES-256'

            # Handle encryption
            if is_encrypted:
                if progress_callback:
                    progress_callback(5, 100, "Backup is encrypted, finding password...")

                password = self._password
                if password is None:
                    password = self._find_password()
                if password is None and password_callback:
                    password = password_callback()
                if password is None:
                    raise AndroidBackupError(
                        "Encrypted backup requires a password. "
                        "Provide password.txt in the backup directory or enter it when prompted."
                    )

                if progress_callback:
                    progress_callback(10, 100, "Decrypting backup...")

                encrypted_data = f.read()
                compressed_data = self._decrypt_payload(encrypted_data, header, password)
            elif header['encryption'] == 'none':
                compressed_data = f.read()
            else:
                raise AndroidBackupError(f"Unknown encryption type: {header['encryption']}")

        # Decompress
        if progress_callback:
            progress_callback(20, 100, "Decompressing backup data...")

        if header['compression'] == 1:
            try:
                tar_data = zlib.decompress(compressed_data)
            except zlib.error as e:
                raise AndroidBackupError(f"Failed to decompress backup: {e}")
        else:
            tar_data = compressed_data

        # Free the compressed data
        del compressed_data

        # Open tar archive
        if progress_callback:
            progress_callback(30, 100, "Parsing tar archive...")

        tar_stream = io.BytesIO(tar_data)
        try:
            tar_handle = tarfile.open(fileobj=tar_stream, mode='r:')
            member_lookup = {m.name: m for m in tar_handle.getmembers()}
        except tarfile.TarError as e:
            raise AndroidBackupError(f"Failed to parse tar data: {e}")

        # Build parsing log
        parsing_log = ParsingLog()
        parsing_log.timestamp = datetime.datetime.now().isoformat()
        parsing_log.total_rows = len(member_lookup)

        # Parse tar members into AndroidBackupFile objects
        if progress_callback:
            progress_callback(40, 100, "Processing backup entries...")

        files = []
        total_members = len(member_lookup)

        for i, (name, member) in enumerate(member_lookup.items()):
            if progress_callback and i % 500 == 0:
                pct = 40 + (i / max(1, total_members)) * 50
                progress_callback(int(pct), 100, f"Processing entries ({i}/{total_members})...")

            domain, token, relative_path = self._parse_tar_path(name)

            if member.isdir():
                parsing_log.add_entry(
                    file_id=name,
                    domain=domain,
                    relative_path=relative_path,
                    status='added_directory',
                    details=f"token={token}" if token else ""
                )
                # Still create a file entry for directories so they appear in stats
                bf = AndroidBackupFile(
                    file_id=name,
                    domain=domain,
                    relative_path=relative_path,
                    file_size=0,
                    mode=member.mode,
                    modified_time=member.mtime if member.mtime else None,
                    flags=2,  # directory
                    token=token,
                )
                files.append(bf)
                continue

            if not member.isfile():
                # Skip symlinks, etc.
                parsing_log.add_entry(
                    file_id=name,
                    domain=domain,
                    relative_path=relative_path,
                    status='skipped_no_content',
                    details=f"Not a regular file (type={member.type})"
                )
                continue

            bf = AndroidBackupFile(
                file_id=name,
                domain=domain,
                relative_path=relative_path,
                file_size=member.size,
                mode=member.mode,
                modified_time=member.mtime if member.mtime else None,
                flags=1,  # file
                actual_file_size=member.size,  # For tar, actual == declared
                token=token,
            )
            files.append(bf)

            status = 'added_file'
            details = f"token={token}" if token else ""
            if token in UNMAPPABLE_TOKENS:
                details += f" (no filesystem equivalent)"
            parsing_log.add_entry(
                file_id=name,
                domain=domain,
                relative_path=relative_path,
                status=status,
                details=details,
                manifest_size=member.size,
            )

        if progress_callback:
            progress_callback(95, 100, "Finalizing...")

        # Try to extract Android version from app manifests
        android_version = ""
        for name, member in member_lookup.items():
            if name.endswith('/_manifest') and member.isfile():
                try:
                    f_obj = tar_handle.extractfile(member)
                    if f_obj:
                        manifest_text = f_obj.read().decode('utf-8', errors='replace')
                        f_obj.close()
                        lines = manifest_text.strip().split('\n')
                        if len(lines) >= 4:
                            # Line 4 is platform SDK version
                            sdk_version = lines[3].strip()
                            if sdk_version.isdigit():
                                android_version = f"SDK {sdk_version}"
                                break
                except Exception:
                    pass

        backup = AndroidBackup(
            path=self.backup_path,
            device_name="Android Device",
            is_encrypted=is_encrypted,
            files=files,
            manifest_db_row_count=total_members,
            parsing_log=parsing_log,
            format_version=header['format_version'],
            android_version=android_version,
            _backup_handle=tar_handle,
            _tar_data=tar_data,
            _member_lookup=member_lookup,
            _password=self._password,
        )

        if progress_callback:
            progress_callback(100, 100, "Android backup loaded")

        return backup

    @staticmethod
    def get_file_content(backup: AndroidBackup, backup_file: AndroidBackupFile) -> Optional[bytes]:
        """
        Get the content of a file from the backup.

        Args:
            backup: The parsed AndroidBackup
            backup_file: The file to extract

        Returns:
            File content as bytes, or None if not available
        """
        if backup_file.is_directory:
            return None

        tar_handle = backup._backup_handle
        if tar_handle is None:
            return None

        try:
            member = backup._member_lookup.get(backup_file.file_id)
            if member is None:
                return None

            f_obj = tar_handle.extractfile(member)
            if f_obj is not None:
                data = f_obj.read()
                f_obj.close()
                return data
            return None
        except Exception:
            return None
