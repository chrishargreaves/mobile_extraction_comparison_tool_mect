"""
ALEX UFED-Style Extraction Parser Module

Handles parsing of ALEX UFED-style logical/advanced logical extractions,
which are ZIP files containing:
- backup/backup.ab: Standard Android .ab backup (compressed, possibly encrypted)
- backup/sdcard/...: Additional sdcard files captured alongside the backup
- sdcard/...: SD card directory listing / files
- logical/Report.xml: Extraction report

The backup.ab is parsed using the standard Android backup parser logic
(header, optional encryption, zlib decompression, tar extraction).
Sdcard entries from backup/sdcard/ and sdcard/ are incorporated as
shared/0 domain entries, deduplicated against the .ab shared entries.

Detection: ZIP file containing 'backup/backup.ab' where the .ab has
the 'ANDROID BACKUP' magic header.
"""

import os
import io
import tarfile
import zlib
import zipfile
import datetime
import configparser
from typing import Dict, List, Optional, Tuple

from android_backup_parser import (
    AndroidBackup, AndroidBackupFile, AndroidBackupParser,
    parse_tar_path, UNMAPPABLE_TOKENS,
)
from ios_backup_parser import ParsingLog


class ALEXParser:
    """Parser for ALEX UFED-style extraction ZIP files."""

    def __init__(self, path: str, password: Optional[str] = None):
        """
        Initialize the parser.

        Args:
            path: Path to the ZIP file or parent directory
            password: Optional password for encrypted .ab backup
        """
        self.path = path
        self._password = password

    @staticmethod
    def is_alex_extraction(path: str) -> bool:
        """Check if a path is an ALEX UFED-style extraction.

        Accepts either:
        - The ZIP file itself
        - The parent directory containing the ZIP + .ufd file
        """
        zip_path = ALEXParser._find_zip(path)
        if not zip_path:
            return False

        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                if 'backup/backup.ab' not in zf.namelist():
                    return False
                # Verify the .ab has the Android backup magic
                ab_data = zf.read('backup/backup.ab')
                return ab_data.startswith(b'ANDROID BACKUP\n')
        except Exception:
            return False

    @staticmethod
    def _find_zip(path: str) -> Optional[str]:
        """Find the extraction ZIP file from a path."""
        if os.path.isfile(path) and zipfile.is_zipfile(path):
            return path
        if os.path.isdir(path):
            for name in os.listdir(path):
                if name.lower().endswith('.zip'):
                    full = os.path.join(path, name)
                    if zipfile.is_zipfile(full):
                        try:
                            with zipfile.ZipFile(full, 'r') as zf:
                                if 'backup/backup.ab' in zf.namelist():
                                    return full
                        except Exception:
                            continue
            # Also check subdirectories (ALEX nests in a timestamped dir)
            for entry in os.listdir(path):
                subdir = os.path.join(path, entry)
                if os.path.isdir(subdir):
                    result = ALEXParser._find_zip(subdir)
                    if result:
                        return result
        return None

    def _read_device_info(self, zip_path: str) -> Tuple[str, str]:
        """Read device name and Android version from the .ufd file.

        Returns:
            (device_name, android_version)
        """
        device_name = "ALEX Extraction"
        android_version = ""

        # Look for .ufd file alongside the ZIP
        parent = os.path.dirname(zip_path)
        for name in os.listdir(parent):
            if name.lower().endswith('.ufd'):
                ufd_path = os.path.join(parent, name)
                try:
                    config = configparser.ConfigParser()
                    config.read(ufd_path, encoding='utf-8')
                    if config.has_section('DeviceInfo'):
                        model = config.get('DeviceInfo', 'Model', fallback='')
                        vendor = config.get('DeviceInfo', 'Vendor', fallback='')
                        if vendor and model:
                            device_name = f"{vendor} {model}"
                        elif model:
                            device_name = model
                        android_version = config.get('DeviceInfo', 'OS', fallback='')
                        if android_version:
                            android_version = f"Android {android_version}"
                except Exception:
                    pass
                break

        return device_name, android_version

    def parse(self, password_callback=None, progress_callback=None) -> AndroidBackup:
        """Parse the ALEX extraction ZIP.

        Returns:
            AndroidBackup object with parsed data from all sources.
        """
        zip_path = self._find_zip(self.path)
        if not zip_path:
            raise RuntimeError(f"No ALEX extraction ZIP found at: {self.path}")

        if progress_callback:
            progress_callback(0, 100, "Opening ALEX extraction ZIP...")

        zf = zipfile.ZipFile(zip_path, 'r')

        parsing_log = ParsingLog()
        parsing_log.timestamp = datetime.datetime.now().isoformat()

        files = []
        seen_domain_paths = set()

        # Source tracking for content extraction:
        #   file_id -> ('ab_tar', TarInfo) | ('zip', zip_entry_name)
        source_lookup = {}

        # --- 1. Parse backup/backup.ab ---
        if progress_callback:
            progress_callback(5, 100, "Extracting backup.ab from ZIP...")

        ab_data = zf.read('backup/backup.ab')

        if progress_callback:
            progress_callback(10, 100, "Parsing Android backup header...")

        # Parse the .ab header and payload
        ab_stream = io.BytesIO(ab_data)
        parser = AndroidBackupParser.__new__(AndroidBackupParser)
        parser.backup_path = zip_path
        parser._password = self._password

        header = parser._parse_header(ab_stream)
        is_encrypted = header['encryption'] == 'AES-256'

        if is_encrypted:
            if progress_callback:
                progress_callback(12, 100, "Backup is encrypted, finding password...")

            password = self._password
            if password is None and password_callback:
                password = password_callback()
            if password is None:
                raise RuntimeError(
                    "Encrypted backup requires a password. "
                    "Provide it when prompted."
                )

            if progress_callback:
                progress_callback(15, 100, "Decrypting backup...")

            encrypted_payload = ab_stream.read()
            compressed_data = parser._decrypt_payload(encrypted_payload, header, password)
        elif header['encryption'] == 'none':
            compressed_data = ab_stream.read()
        else:
            raise RuntimeError(f"Unknown encryption type: {header['encryption']}")

        # Decompress
        if progress_callback:
            progress_callback(20, 100, "Decompressing backup data...")

        if header['compression'] == 1:
            try:
                tar_data = zlib.decompress(compressed_data)
            except zlib.error as e:
                raise RuntimeError(f"Failed to decompress backup: {e}")
        else:
            tar_data = compressed_data

        del compressed_data

        # Open tar archive
        if progress_callback:
            progress_callback(30, 100, "Parsing tar archive from backup.ab...")

        tar_stream = io.BytesIO(tar_data)
        try:
            tar_handle = tarfile.open(fileobj=tar_stream, mode='r:')
            member_lookup = {m.name: m for m in tar_handle.getmembers()}
        except tarfile.TarError as e:
            raise RuntimeError(f"Failed to parse tar data from backup.ab: {e}")

        if progress_callback:
            progress_callback(35, 100, f"Processing backup.ab ({len(member_lookup)} entries)...")

        android_version_from_ab = ""
        for i, (name, member) in enumerate(member_lookup.items()):
            if progress_callback and i % 500 == 0:
                pct = 35 + (i / max(1, len(member_lookup))) * 30
                progress_callback(int(pct), 100, f"Processing backup.ab: {i}/{len(member_lookup)}")

            domain, token, relative_path = parse_tar_path(name)

            is_dir = member.isdir()
            if not is_dir and not member.isfile():
                parsing_log.add_entry(
                    file_id=name, domain=domain, relative_path=relative_path,
                    status='skipped_no_content',
                    details=f"Not a regular file (type={member.type})",
                )
                continue

            mode = member.mode
            if is_dir and not (mode & 0o170000):
                mode |= 0o040000

            bf = AndroidBackupFile(
                file_id=name,
                domain=domain,
                relative_path=relative_path,
                file_size=0 if is_dir else member.size,
                mode=mode,
                modified_time=member.mtime if member.mtime else None,
                flags=2 if is_dir else 1,
                actual_file_size=member.size if not is_dir else None,
                token=token,
            )
            files.append(bf)
            seen_domain_paths.add(bf.full_domain_path)
            source_lookup[name] = ('ab_tar', member)

            status = 'added_directory' if is_dir else 'added_file'
            details = f"token={token}" if token else ""
            if token in UNMAPPABLE_TOKENS:
                details += " (no filesystem equivalent)"
            parsing_log.add_entry(
                file_id=name, domain=domain, relative_path=relative_path,
                status=status, details=details,
                manifest_size=member.size if not is_dir else 0,
            )

            # Extract Android version from first _manifest
            if not android_version_from_ab and name.endswith('/_manifest') and member.isfile():
                try:
                    f_obj = tar_handle.extractfile(member)
                    if f_obj:
                        text = f_obj.read().decode('utf-8', errors='replace')
                        f_obj.close()
                        lines = text.strip().split('\n')
                        if len(lines) >= 4 and lines[3].strip().isdigit():
                            android_version_from_ab = f"SDK {lines[3].strip()}"
                except Exception:
                    pass

        # --- 2. Parse backup/sdcard/ entries from ZIP (extra sdcard files) ---
        if progress_callback:
            progress_callback(70, 100, "Processing sdcard entries...")

        backup_sdcard_entries = [
            n for n in zf.namelist()
            if n.startswith('backup/sdcard/') and n != 'backup/sdcard/'
        ]
        sdcard_entries = [
            n for n in zf.namelist()
            if n.startswith('sdcard/') and n != 'sdcard/'
        ]

        sdcard_added = 0
        for name in backup_sdcard_entries + sdcard_entries:
            info = zf.getinfo(name)

            # Strip prefix to get sdcard-relative path
            if name.startswith('backup/sdcard/'):
                rel = name[len('backup/sdcard/'):]
            elif name.startswith('sdcard/'):
                rel = name[len('sdcard/'):]
            else:
                continue

            if not rel:
                continue

            domain = 'shared/0'
            is_dir = name.endswith('/')
            domain_path = f"{domain}/{rel.rstrip('/')}" if rel.rstrip('/') else domain

            # Skip if already seen from .ab shared entries or earlier sdcard entry
            if domain_path in seen_domain_paths:
                continue

            file_id = f"zip:{name}"
            bf = AndroidBackupFile(
                file_id=file_id,
                domain=domain,
                relative_path=rel.rstrip('/'),
                file_size=0 if is_dir else info.file_size,
                mode=0o040755 if is_dir else 0o100644,
                flags=2 if is_dir else 1,
                actual_file_size=info.file_size if not is_dir else None,
                token='',
            )
            files.append(bf)
            seen_domain_paths.add(domain_path)
            source_lookup[file_id] = ('zip', name)
            sdcard_added += 1

            parsing_log.add_entry(
                file_id=file_id, domain=domain, relative_path=rel.rstrip('/'),
                status='added_directory' if is_dir else 'added_file',
                details=f"from {name.split('/')[0]}/ in ZIP",
                manifest_size=info.file_size if not is_dir else 0,
            )

        if progress_callback and sdcard_added:
            progress_callback(80, 100, f"Added {sdcard_added} extra sdcard files from ZIP")

        # --- 3. Read device info from .ufd file ---
        device_name, android_version = self._read_device_info(zip_path)
        # Prefer .ufd version, fall back to manifest
        if not android_version and android_version_from_ab:
            android_version = android_version_from_ab

        parsing_log.total_rows = len(files)

        if progress_callback:
            progress_callback(90, 100, "Finalizing...")

        backup = AndroidBackup(
            path=zip_path,
            device_name=device_name,
            is_encrypted=is_encrypted,
            files=files,
            manifest_db_row_count=len(files),
            parsing_log=parsing_log,
            format_version=header.get('format_version', 0),
            android_version=android_version,
            backup_type='android',
            _backup_handle=tar_handle,
            _tar_data=tar_data,
            _member_lookup=member_lookup,
        )

        # Attach extra handles for content extraction
        backup._alex_source_lookup = source_lookup
        backup._alex_zip = zf

        if progress_callback:
            progress_callback(100, 100, "ALEX extraction loaded")

        return backup

    @staticmethod
    def get_file_content(backup: AndroidBackup, backup_file: AndroidBackupFile) -> Optional[bytes]:
        """Get file content from the appropriate source within the ALEX extraction."""
        if backup_file.is_directory:
            return None

        source_lookup = getattr(backup, '_alex_source_lookup', None)
        if not source_lookup:
            return None

        entry = source_lookup.get(backup_file.file_id)
        if not entry:
            return None

        source_type, source_ref = entry
        try:
            if source_type == 'ab_tar':
                f_obj = backup._backup_handle.extractfile(source_ref)
                if f_obj:
                    data = f_obj.read()
                    f_obj.close()
                    return data
            elif source_type == 'zip':
                alex_zip = getattr(backup, '_alex_zip', None)
                if alex_zip:
                    return alex_zip.read(source_ref)
        except Exception:
            pass

        return None
