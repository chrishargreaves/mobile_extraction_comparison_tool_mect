"""
Magnet Acquire Quick Image Parser Module

Handles parsing of Magnet Acquire Quick Image ZIP files, which contain:
- adb-data.tar: Standard ADB backup tar (apps/<pkg>/<token>/<path> + shared/)
- sdcard.tar.gz: Gzipped tar of /sdcard/ contents
- storage-sdcard0.tar.gz / storage-sdcard1.tar.gz: Usually empty
- Live Data/: Dumpsys text files and agent-captured databases

The adb-data.tar is a raw tar (no ANDROID BACKUP header or zlib wrapper).
"""

import os
import io
import tarfile
import zipfile
import datetime
from dataclasses import field
from typing import Dict, List, Optional, Tuple

from android_backup_parser import (
    AndroidBackup, AndroidBackupFile, parse_tar_path,
    UNMAPPABLE_TOKENS,
)
from ios_backup_parser import ParsingLog


# Domains that cannot be mapped to filesystem paths
UNMAPPABLE_DOMAINS = {'Live Data'}


class MagnetQuickImageParser:
    """Parser for Magnet Acquire Quick Image ZIP files."""

    def __init__(self, zip_path: str):
        self.zip_path = zip_path

    @staticmethod
    def is_magnet_quick_image(path: str) -> bool:
        """Check if a path is a Magnet Acquire Quick Image.

        Accepts either:
        - The ZIP file itself
        - The parent directory containing the ZIP + image_info.txt
        """
        if os.path.isfile(path) and zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path, 'r') as zf:
                    return 'adb-data.tar' in zf.namelist()
            except Exception:
                return False

        if os.path.isdir(path):
            # Look for a ZIP containing adb-data.tar
            for name in os.listdir(path):
                if name.lower().endswith('.zip'):
                    full = os.path.join(path, name)
                    if MagnetQuickImageParser.is_magnet_quick_image(full):
                        return True
        return False

    @staticmethod
    def find_zip_in_dir(path: str) -> Optional[str]:
        """If path is a directory, find the Quick Image ZIP inside it."""
        if os.path.isfile(path):
            return path
        if os.path.isdir(path):
            for name in os.listdir(path):
                if name.lower().endswith('.zip'):
                    full = os.path.join(path, name)
                    if MagnetQuickImageParser.is_magnet_quick_image(full):
                        return full
        return None

    def parse(self, password_callback=None, progress_callback=None) -> AndroidBackup:
        """Parse the Magnet Quick Image ZIP.

        Returns:
            AndroidBackup object with parsed data from all sources.
        """
        zip_path = self.find_zip_in_dir(self.zip_path)
        if not zip_path:
            raise RuntimeError(f"No Magnet Quick Image ZIP found at: {self.zip_path}")

        if progress_callback:
            progress_callback(0, 100, "Opening Magnet Quick Image ZIP...")

        zf = zipfile.ZipFile(zip_path, 'r')

        parsing_log = ParsingLog()
        parsing_log.timestamp = datetime.datetime.now().isoformat()

        files = []
        # Track seen paths to deduplicate sdcard vs shared/0
        seen_domain_paths = set()

        # Source tracking for content extraction:
        #   file_id -> ('adb_tar', TarInfo) or ('sdcard_tar', TarInfo) or ('zip', zip_entry_name)
        source_lookup = {}

        # --- 1. Parse adb-data.tar ---
        if progress_callback:
            progress_callback(5, 100, "Reading adb-data.tar...")

        adb_tar_data = zf.read('adb-data.tar')
        adb_tar_stream = io.BytesIO(adb_tar_data)
        adb_tar = tarfile.open(fileobj=adb_tar_stream, mode='r:')
        adb_members = {m.name: m for m in adb_tar.getmembers()}

        if progress_callback:
            progress_callback(20, 100, f"Processing adb-data.tar ({len(adb_members)} entries)...")

        android_version = ""
        for i, (name, member) in enumerate(adb_members.items()):
            if progress_callback and i % 500 == 0:
                pct = 20 + (i / max(1, len(adb_members))) * 40
                progress_callback(int(pct), 100, f"Processing adb-data: {i}/{len(adb_members)}")

            domain, token, relative_path = parse_tar_path(name)

            is_dir = member.isdir()
            mode = member.mode
            # Ensure directory type bit is set in mode for proper is_directory detection
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
            source_lookup[name] = ('adb_tar', member)

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
            if not android_version and name.endswith('/_manifest') and member.isfile():
                try:
                    f_obj = adb_tar.extractfile(member)
                    if f_obj:
                        text = f_obj.read().decode('utf-8', errors='replace')
                        f_obj.close()
                        lines = text.strip().split('\n')
                        if len(lines) >= 4 and lines[3].strip().isdigit():
                            android_version = f"SDK {lines[3].strip()}"
                except Exception:
                    pass

        # --- 2. Parse sdcard.tar.gz (extra sdcard files not in shared/0) ---
        sdcard_tar = None
        sdcard_tar_data = None
        sdcard_tar_stream = None
        if 'sdcard.tar.gz' in zf.namelist():
            if progress_callback:
                progress_callback(60, 100, "Reading sdcard.tar.gz...")

            sdcard_tar_data = zf.read('sdcard.tar.gz')
            if len(sdcard_tar_data) > 29:  # Skip empty archives
                sdcard_tar_stream = io.BytesIO(sdcard_tar_data)
                sdcard_tar = tarfile.open(fileobj=sdcard_tar_stream, mode='r:gz')
                sdcard_members = {m.name: m for m in sdcard_tar.getmembers()}

                added = 0
                for name, member in sdcard_members.items():
                    # Strip "sdcard/" prefix and map to shared/0 domain
                    stripped = name.lstrip('./')
                    if stripped.startswith('sdcard/'):
                        rel = stripped[len('sdcard/'):]
                    elif stripped == 'sdcard':
                        rel = ''
                    else:
                        rel = stripped

                    domain = 'shared/0'
                    domain_path = f"{domain}/{rel}" if rel else domain

                    # Skip if already seen from adb-data.tar
                    if domain_path in seen_domain_paths:
                        continue

                    is_dir = member.isdir()
                    mode = member.mode
                    if is_dir and not (mode & 0o170000):
                        mode |= 0o040000
                    file_id = f"sdcard_tar:{name}"
                    bf = AndroidBackupFile(
                        file_id=file_id,
                        domain=domain,
                        relative_path=rel,
                        file_size=0 if is_dir else member.size,
                        mode=mode,
                        modified_time=member.mtime if member.mtime else None,
                        flags=2 if is_dir else 1,
                        actual_file_size=member.size if not is_dir else None,
                        token='',
                    )
                    files.append(bf)
                    seen_domain_paths.add(domain_path)
                    source_lookup[file_id] = ('sdcard_tar', member)
                    added += 1

                    parsing_log.add_entry(
                        file_id=file_id, domain=domain, relative_path=rel,
                        status='added_directory' if is_dir else 'added_file',
                        details="from sdcard.tar.gz",
                        manifest_size=member.size if not is_dir else 0,
                    )

                if progress_callback:
                    progress_callback(70, 100, f"Added {added} extra files from sdcard.tar.gz")

        # --- 3. Parse Live Data/ entries from ZIP ---
        if progress_callback:
            progress_callback(75, 100, "Processing Live Data...")

        live_data_entries = [n for n in zf.namelist() if n.startswith('Live Data/')]
        for name in live_data_entries:
            info = zf.getinfo(name)
            # Strip "Live Data/" prefix for relative path
            rel = name[len('Live Data/'):]
            if not rel:
                continue

            is_dir = name.endswith('/')
            file_id = f"zip:{name}"
            bf = AndroidBackupFile(
                file_id=file_id,
                domain='Live Data',
                relative_path=rel.rstrip('/'),
                file_size=0 if is_dir else info.file_size,
                mode=0o040755 if is_dir else 0o100644,
                flags=2 if is_dir else 1,
                actual_file_size=info.file_size if not is_dir else None,
                token='',
            )
            files.append(bf)
            source_lookup[file_id] = ('zip', name)

            parsing_log.add_entry(
                file_id=file_id, domain='Live Data', relative_path=rel.rstrip('/'),
                status='added_directory' if is_dir else 'added_file',
                details="Live Data (agent-captured, not mappable)",
            )

        # --- 4. Extract device info from image_info.txt ---
        device_name = "Magnet Quick Image"
        parent_dir = os.path.dirname(zip_path)
        info_path = os.path.join(parent_dir, 'image_info.txt')
        if os.path.isfile(info_path):
            try:
                with open(info_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if line.startswith('Product Model:'):
                            device_name = line.split(':', 1)[1].strip()
                        elif line.startswith('Operating System Version:') and not android_version:
                            android_version = f"Android {line.split(':', 1)[1].strip()}"
            except Exception:
                pass

        parsing_log.total_rows = len(files)

        if progress_callback:
            progress_callback(90, 100, "Finalizing...")

        backup = AndroidBackup(
            path=zip_path,
            device_name=device_name,
            is_encrypted=False,
            files=files,
            manifest_db_row_count=len(files),
            parsing_log=parsing_log,
            format_version=0,
            android_version=android_version,
            backup_type='android',
            _backup_handle=adb_tar,
            _tar_data=adb_tar_data,
            _member_lookup={},  # Not used directly; we use source_lookup instead
        )

        # Attach extra handles for content extraction
        backup._magnet_source_lookup = source_lookup
        backup._magnet_sdcard_tar = sdcard_tar
        backup._magnet_sdcard_tar_data = sdcard_tar_data
        backup._magnet_zip = zf

        if progress_callback:
            progress_callback(100, 100, "Magnet Quick Image loaded")

        return backup

    @staticmethod
    def get_file_content(backup: AndroidBackup, backup_file: AndroidBackupFile) -> Optional[bytes]:
        """Get file content from the appropriate source within the Magnet image."""
        if backup_file.is_directory:
            return None

        source_lookup = getattr(backup, '_magnet_source_lookup', None)
        if not source_lookup:
            return None

        entry = source_lookup.get(backup_file.file_id)
        if not entry:
            return None

        source_type, source_ref = entry
        try:
            if source_type == 'adb_tar':
                f_obj = backup._backup_handle.extractfile(source_ref)
                if f_obj:
                    data = f_obj.read()
                    f_obj.close()
                    return data
            elif source_type == 'sdcard_tar':
                sdcard_tar = getattr(backup, '_magnet_sdcard_tar', None)
                if sdcard_tar:
                    f_obj = sdcard_tar.extractfile(source_ref)
                    if f_obj:
                        data = f_obj.read()
                        f_obj.close()
                        return data
            elif source_type == 'zip':
                magnet_zip = getattr(backup, '_magnet_zip', None)
                if magnet_zip:
                    return magnet_zip.read(source_ref)
        except Exception:
            pass

        return None
