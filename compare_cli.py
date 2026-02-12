#!/usr/bin/env python3
"""
CLI tool for comparing mobile backups to filesystem acquisitions.

Supports both iOS backups (directory/ZIP with Manifest.db) and
Android backups (.ab files). Backup type is auto-detected.

Usage:
    python compare_cli.py <backup_path> <filesystem_path> [options]

Examples:
    # Basic comparison with summary output
    python compare_cli.py ./backup ./filesystem.tar

    # Android backup comparison
    python compare_cli.py ./backup.ab ./filesystem.tar

    # Detailed output with unmapped files
    python compare_cli.py ./backup ./filesystem.tar --output detailed

    # JSON output for further processing
    python compare_cli.py ./backup ./filesystem.tar --output json > results.json

    # CSV of unmapped files
    python compare_cli.py ./backup ./filesystem.tar --output csv-unmapped > unmapped.csv
"""

import argparse
import getpass
import json
import sys
from typing import Optional

from ios_backup_parser import iOSBackupParser
from android_backup_parser import AndroidBackupParser
from filesystem_loader import FilesystemLoader
from path_mapper import PathMapper, MappingStatus
from android_path_mapper import AndroidPathMapper
from filesystem_mapper import FilesystemMapper, FilesystemAsBackup
from alex_parser import ALEXParser


def print_progress(current: int, total: int, message: str):
    """Print progress to stderr so it doesn't interfere with output."""
    if total > 0:
        percent = current / total * 100
        print(f"\r{message} ({percent:.1f}%)", end="", file=sys.stderr)
    else:
        print(f"\r{message}", end="", file=sys.stderr)


def prompt_password():
    """Prompt the user for a backup password via stdin."""
    return getpass.getpass("Backup is encrypted. Enter password: ")


def load_backup(backup_path: str):
    """Load and parse a backup (auto-detects iOS vs Android)."""
    print(f"Loading backup from: {backup_path}", file=sys.stderr)

    if iOSBackupParser.is_ios_backup(backup_path):
        parser = iOSBackupParser(backup_path)
        backup = parser.parse(password_callback=prompt_password)
        print(f"  Type: iOS", file=sys.stderr)
        print(f"  Device: {backup.device_name} ({backup.product_type})", file=sys.stderr)
        print(f"  iOS Version: {backup.ios_version}", file=sys.stderr)
    elif AndroidBackupParser.is_android_backup(backup_path):
        parser = AndroidBackupParser(backup_path)
        backup = parser.parse(password_callback=prompt_password)
        print(f"  Type: Android", file=sys.stderr)
        print(f"  Device: {backup.device_name}", file=sys.stderr)
        if backup.android_version:
            print(f"  Android: {backup.android_version}", file=sys.stderr)
    elif ALEXParser.is_alex_extraction(backup_path):
        parser = ALEXParser(backup_path)
        backup = parser.parse(password_callback=prompt_password, progress_callback=print_progress)
        print(f"\n  Type: ALEX UFED-style extraction", file=sys.stderr)
        print(f"  Device: {backup.device_name}", file=sys.stderr)
        if backup.android_version:
            print(f"  Android: {backup.android_version}", file=sys.stderr)
    else:
        # Try as plain filesystem archive
        try:
            loader = FilesystemLoader(backup_path, progress_callback=print_progress)
            acquisition = loader.load()
            backup = FilesystemAsBackup(acquisition)
            print(f"\n  Type: Filesystem archive ({acquisition.format})", file=sys.stderr)
            print(f"  Platform: {acquisition.platform}", file=sys.stderr)
        except Exception:
            raise ValueError(
                f"Not a recognized backup format: {backup_path}\n"
                "Supported: iOS backup (directory/ZIP), Android backup (.ab), "
                "or plain archive (ZIP, TAR, TAR.GZ, directory)"
            )

    print(f"  Encrypted: {backup.is_encrypted}", file=sys.stderr)
    print(f"  Files: {len(backup.files)}", file=sys.stderr)

    return backup


def load_filesystem(filesystem_path: str):
    """Load a filesystem acquisition."""
    print(f"\nLoading filesystem from: {filesystem_path}", file=sys.stderr)

    loader = FilesystemLoader(filesystem_path, progress_callback=print_progress)
    filesystem = loader.load()

    print(f"\n  Files: {len(filesystem.files)}", file=sys.stderr)
    print(f"  Container mappings: {len(filesystem.app_container_mapping)} apps, "
          f"{len(filesystem.group_container_mapping)} groups", file=sys.stderr)

    return filesystem


def run_comparison(backup, filesystem):
    """Run the path mapping comparison."""
    print("\nMapping paths...", file=sys.stderr)

    if hasattr(backup, 'backup_type') and backup.backup_type == 'filesystem':
        mapper = FilesystemMapper(backup, filesystem)
    elif hasattr(backup, 'backup_type') and backup.backup_type == 'android':
        mapper = AndroidPathMapper(backup, filesystem)
    else:
        mapper = PathMapper(backup, filesystem)

    mapper.map_all()

    print("  Done.", file=sys.stderr)

    return mapper


def output_summary(mapper: PathMapper):
    """Output a summary of the comparison."""
    stats = mapper.statistics

    print("\n" + "=" * 60)
    print("COMPARISON SUMMARY")
    print("=" * 60)

    print(f"\nBackup:")
    print(f"  Total files: {stats.total_backup_files:,}")
    print(f"  Total directories: {stats.total_backup_directories:,}")

    print(f"\nFilesystem:")
    print(f"  Total files: {stats.total_filesystem_files:,}")
    print(f"  Total directories: {stats.total_filesystem_directories:,}")

    print(f"\nMapping Results:")
    print(f"  Successfully mapped: {stats.mapped_files:,}")
    print(f"  Not found in filesystem: {stats.not_found_files:,}")
    print(f"  Unmappable (unknown domain): {stats.unmappable_files:,}")

    print(f"\nCoverage Analysis:")
    print(f"  Files only in backup: {stats.backup_only_files:,}")
    print(f"  Files only in filesystem: {stats.filesystem_only_files:,}")
    print(f"  Backup coverage of filesystem: {stats.backup_coverage_percent:.1f}%")

    # Domain breakdown
    print(f"\nBy Domain:")
    by_domain = mapper.get_mappings_by_domain()
    for domain in sorted(by_domain.keys()):
        domain_mappings = by_domain[domain]
        mapped = sum(1 for m in domain_mappings if m.status == MappingStatus.MAPPED)
        total = len(domain_mappings)
        pct = (mapped / total * 100) if total > 0 else 0
        print(f"  {domain}: {mapped}/{total} ({pct:.1f}%)")


def output_detailed(mapper: PathMapper):
    """Output detailed comparison with unmapped files."""
    output_summary(mapper)

    # Unmapped backup files
    unmapped = mapper.get_unmapped_backup_files()
    if unmapped:
        print("\n" + "=" * 60)
        print(f"UNMAPPED BACKUP FILES ({len(unmapped)})")
        print("=" * 60)
        for bf in unmapped[:100]:  # Limit to first 100
            print(f"  {bf.domain}/{bf.relative_path}")
        if len(unmapped) > 100:
            print(f"  ... and {len(unmapped) - 100} more")

    # Filesystem-only files
    fs_only = mapper.get_filesystem_files_not_in_backup()
    if fs_only:
        print("\n" + "=" * 60)
        print(f"FILES ONLY IN FILESYSTEM ({len(fs_only)})")
        print("=" * 60)
        for ff in fs_only[:100]:  # Limit to first 100
            print(f"  {ff.path}")
        if len(fs_only) > 100:
            print(f"  ... and {len(fs_only) - 100} more")


def output_json(mapper, backup, filesystem):
    """Output comparison results as JSON."""
    stats = mapper.statistics

    backup_info = {
        "path": backup.path,
        "device_name": backup.device_name,
        "is_encrypted": backup.is_encrypted,
        "total_files": stats.total_backup_files,
        "total_directories": stats.total_backup_directories,
    }
    if hasattr(backup, 'backup_type'):
        backup_info["backup_type"] = backup.backup_type
    if backup.ios_version:
        backup_info["ios_version"] = backup.ios_version
    if hasattr(backup, 'android_version') and backup.android_version:
        backup_info["android_version"] = backup.android_version
    if backup.product_type:
        backup_info["product_type"] = backup.product_type

    result = {
        "backup": backup_info,
        "filesystem": {
            "path": filesystem.path,
            "platform": filesystem.platform,
            "total_files": stats.total_filesystem_files,
            "total_directories": stats.total_filesystem_directories,
            "app_containers": len(filesystem.app_container_mapping),
            "group_containers": len(filesystem.group_container_mapping)
        },
        "mapping": {
            "mapped_files": stats.mapped_files,
            "not_found_files": stats.not_found_files,
            "unmappable_files": stats.unmappable_files,
            "backup_only_files": stats.backup_only_files,
            "filesystem_only_files": stats.filesystem_only_files,
            "backup_coverage_percent": round(stats.backup_coverage_percent, 2)
        },
        "by_domain": {}
    }

    # Domain breakdown
    by_domain = mapper.get_mappings_by_domain()
    for domain in sorted(by_domain.keys()):
        domain_mappings = by_domain[domain]
        mapped = sum(1 for m in domain_mappings if m.status == MappingStatus.MAPPED)
        total = len(domain_mappings)
        result["by_domain"][domain] = {
            "total": total,
            "mapped": mapped,
            "coverage_percent": round((mapped / total * 100) if total > 0 else 0, 2)
        }

    print(json.dumps(result, indent=2))


def output_csv_unmapped(mapper: PathMapper):
    """Output unmapped backup files as CSV."""
    print("domain,relative_path,file_size,status,notes")

    for mapping in mapper.mappings:
        if mapping.status in (MappingStatus.NOT_FOUND, MappingStatus.UNMAPPABLE):
            bf = mapping.backup_file
            # Escape quotes in fields
            domain = bf.domain.replace('"', '""')
            path = bf.relative_path.replace('"', '""')
            notes = mapping.notes.replace('"', '""')
            print(f'"{domain}","{path}",{bf.file_size},"{mapping.status.value}","{notes}"')


def output_csv_filesystem_only(mapper: PathMapper):
    """Output filesystem-only files as CSV."""
    print("path,size,is_directory")

    for ff in mapper.get_filesystem_files_not_in_backup():
        path = ff.path.replace('"', '""')
        print(f'"{path}",{ff.size},{ff.is_directory}')


def output_csv_all_mappings(mapper: PathMapper):
    """Output all mappings as CSV."""
    print("domain,relative_path,filesystem_path,status,file_size,notes")

    for mapping in mapper.mappings:
        bf = mapping.backup_file
        domain = bf.domain.replace('"', '""')
        rel_path = bf.relative_path.replace('"', '""')
        fs_path = (mapping.filesystem_path or "").replace('"', '""')
        notes = mapping.notes.replace('"', '""')
        print(f'"{domain}","{rel_path}","{fs_path}","{mapping.status.value}",{bf.file_size},"{notes}"')


def output_domain_mappings(mapper: PathMapper, filesystem):
    """Output domain to filesystem path mappings (container resolution)."""
    print("\n" + "=" * 60)
    print("DOMAIN TO FILESYSTEM PATH MAPPINGS")
    print("=" * 60)

    # Collect unique domain -> base path mappings
    domain_paths = {}

    for mapping in mapper.mappings:
        bf = mapping.backup_file
        domain = bf.domain

        if domain not in domain_paths and mapping.filesystem_path:
            # Extract the base path (without the relative file path)
            if bf.relative_path and mapping.filesystem_path.endswith(bf.relative_path):
                base_path = mapping.filesystem_path[:-len(bf.relative_path)].rstrip('/')
            else:
                base_path = mapping.filesystem_path
            domain_paths[domain] = base_path

    # Group by base domain type
    app_domains = {}
    group_domains = {}
    plugin_domains = {}
    system_domains = {}
    other_domains = {}

    for domain, path in sorted(domain_paths.items()):
        if domain.startswith('AppDomainGroup-'):
            group_domains[domain] = path
        elif domain.startswith('AppDomainPlugin-'):
            plugin_domains[domain] = path
        elif domain.startswith('AppDomain-'):
            app_domains[domain] = path
        elif domain.startswith('SysContainerDomain-') or domain.startswith('SysSharedContainerDomain-'):
            system_domains[domain] = path
        else:
            other_domains[domain] = path

    # Output by category
    if other_domains:
        print("\nStandard Domains:")
        for domain, path in sorted(other_domains.items()):
            print(f"  {domain}")
            print(f"    -> {path}")

    if app_domains:
        print(f"\nApp Domains ({len(app_domains)}):")
        for domain, path in sorted(app_domains.items()):
            bundle_id = domain.split('-', 1)[1] if '-' in domain else domain
            print(f"  {bundle_id}")
            print(f"    -> {path}")

    if group_domains:
        print(f"\nApp Group Domains ({len(group_domains)}):")
        for domain, path in sorted(group_domains.items()):
            bundle_id = domain.split('-', 1)[1] if '-' in domain else domain
            print(f"  {bundle_id}")
            print(f"    -> {path}")

    if plugin_domains:
        print(f"\nPlugin Domains ({len(plugin_domains)}):")
        for domain, path in sorted(plugin_domains.items()):
            bundle_id = domain.split('-', 1)[1] if '-' in domain else domain
            print(f"  {bundle_id}")
            print(f"    -> {path}")

    if system_domains:
        print(f"\nSystem Container Domains ({len(system_domains)}):")
        for domain, path in sorted(system_domains.items()):
            print(f"  {domain}")
            print(f"    -> {path}")

    # Also show container resolution from filesystem
    print("\n" + "-" * 60)
    print("Container GUID Resolution (from filesystem metadata):")
    print("-" * 60)

    if filesystem.app_container_mapping:
        print(f"\nApp Containers ({len(filesystem.app_container_mapping)}):")
        for bundle_id, guid in sorted(filesystem.app_container_mapping.items())[:20]:
            print(f"  {bundle_id} -> {guid}")
        if len(filesystem.app_container_mapping) > 20:
            print(f"  ... and {len(filesystem.app_container_mapping) - 20} more")

    if filesystem.group_container_mapping:
        print(f"\nGroup Containers ({len(filesystem.group_container_mapping)}):")
        for bundle_id, guid in sorted(filesystem.group_container_mapping.items())[:20]:
            print(f"  {bundle_id} -> {guid}")
        if len(filesystem.group_container_mapping) > 20:
            print(f"  ... and {len(filesystem.group_container_mapping) - 20} more")


def main():
    parser = argparse.ArgumentParser(
        description="Compare mobile backup to filesystem acquisition (iOS and Android)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Supported backup formats:
  iOS:     Directory with Manifest.db, or ZIP archive
  Android: .ab backup file (encrypted or unencrypted)

Output formats:
  stats            Coverage statistics summary (default)
  detailed         Stats plus lists of unmapped files
  domains          Domain to filesystem path mappings
  json             Full results as JSON
  csv-unmapped     Unmapped backup files as CSV
  csv-fs-only      Filesystem-only files as CSV
  csv-all          All mappings as CSV

Examples:
  %(prog)s ./backup ./filesystem.tar
  %(prog)s ./backup.ab ./filesystem.tar
  %(prog)s ./backup ./filesystem.tar --output domains
  %(prog)s ./backup ./filesystem.tar --output json > results.json
  %(prog)s ./backup ./filesystem.tar -o csv-unmapped > unmapped.csv
        """
    )

    parser.add_argument("backup_path", help="Path to backup (iOS directory/ZIP, Android .ab, or plain archive/directory)")
    parser.add_argument("filesystem_path", help="Path to filesystem acquisition (TAR, ZIP, or directory)")
    parser.add_argument("-o", "--output",
                        choices=["stats", "detailed", "domains", "json", "csv-unmapped", "csv-fs-only", "csv-all"],
                        default="stats",
                        help="Output format (default: stats)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress progress messages")

    args = parser.parse_args()

    try:
        # Load data
        if args.quiet:
            # Redirect stderr to devnull for quiet mode
            import os
            sys.stderr = open(os.devnull, 'w')

        backup = load_backup(args.backup_path)
        filesystem = load_filesystem(args.filesystem_path)
        mapper = run_comparison(backup, filesystem)

        # Restore stderr if we suppressed it
        if args.quiet:
            sys.stderr = sys.__stderr__

        # Output results
        if args.output == "stats":
            output_summary(mapper)
        elif args.output == "detailed":
            output_detailed(mapper)
        elif args.output == "domains":
            output_domain_mappings(mapper, filesystem)
        elif args.output == "json":
            output_json(mapper, backup, filesystem)
        elif args.output == "csv-unmapped":
            output_csv_unmapped(mapper)
        elif args.output == "csv-fs-only":
            output_csv_filesystem_only(mapper)
        elif args.output == "csv-all":
            output_csv_all_mappings(mapper)

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
