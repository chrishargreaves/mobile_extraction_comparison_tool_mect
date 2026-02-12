⚠️ **Note this is an AI generated application.** - see credits and disclaimer below.

# MECT — Mobile Extraction Comparison Tool

A tool for comparing mobile device backups against filesystem acquisitions. In order to help with learning about the relationships between different backup formats, MECT also allows you to select a file in a backup and it will locate that file in the file system extraction. 

Provides both a Tkinter GUI and a command-line interface.

<img width="1140" height="720" alt="image" src="https://github.com/user-attachments/assets/8df13850-baa8-4259-9ef3-fc91ba53da67" />


# Credits and Disclaimer

- software conceptualization, design and architecture - Chris Hargreaves
- code - Claude Opus 4.5/4.6


## Supported Formats

**Backups (left pane / first argument):**

| Format | Description |
|---|---|
| iOS backup directory | Folder containing `Manifest.db` (standard iTunes/Finder backup) |
| iOS backup ZIP | Zipped iTunes backup |
| Android `.ab` file | AES-256 encrypted or unencrypted Android backup |
| Magnet Acquire Quick Image (Android) | ZIP containing `adb-data.tar` (or directory containing such a ZIP) |
| Magnet Acquire Quick Image (iOS) | ZIP containing `Manifest.db` plus optional `Filesystem/` and `Live Data/` folders |

**Filesystem acquisitions (right pane / second argument):**

| Format | Description |
|---|---|
| TAR archive | Any compression supported; platform auto-detected from paths |
| ZIP archive | Platform auto-detected |
| Extracted directory | Walked recursively; platform auto-detected |

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

### GUI

```bash
python main.py
```

Load a backup and filesystem acquisition using the toolbar buttons, then run the comparison to see mapped, unmapped, and filesystem-only files.

### CLI

```bash
python compare_cli.py <backup_path> <filesystem_path> [options]
```

**Options:**

| Flag | Description |
|---|---|
| `-o`, `--output` | Output format: `stats` (default), `detailed`, `domains`, `json`, `csv-unmapped`, `csv-fs-only`, `csv-all` |
| `-q`, `--quiet` | Suppress progress messages |

**Examples:**

```bash
# Basic comparison with summary stats
python compare_cli.py ./backup ./filesystem.tar

# Android encrypted backup (will prompt for password if needed)
python compare_cli.py ./backup.ab ./filesystem.tar

# Export all mappings as CSV
python compare_cli.py ./backup ./filesystem.tar -o csv-all > mappings.csv

# Full JSON output
python compare_cli.py ./backup ./filesystem.tar -o json > results.json
```

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

## Architecture

- `main.py` — Tkinter GUI application
- `compare_cli.py` — Command-line interface
- `ios_backup_parser.py` — iOS backup parsing (encrypted and unencrypted)
- `android_backup_parser.py` — Android `.ab` backup parsing
- `magnet_parser.py` — Magnet Acquire Quick Image parsing
- `filesystem_loader.py` — Filesystem acquisition loading (TAR, ZIP, directory)
- `path_mapper.py` — iOS backup-to-filesystem path mapping
- `android_path_mapper.py` — Android backup-to-filesystem path mapping
