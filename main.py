#!/usr/bin/env python3
"""
Mobile Backup to Filesystem Comparison Tool

A GUI application for comparing mobile backup data (iOS or Android) to
filesystem acquisitions, helping forensic analysts understand the mapping
between backup structures and the actual device filesystem.
"""

import os
import sys
import hashlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import threading

from ios_backup_parser import iOSBackupParser, iOSBackup, BackupFile
from android_backup_parser import AndroidBackupParser, AndroidBackup, AndroidBackupFile
from filesystem_loader import FilesystemLoader, FilesystemAcquisition, FilesystemFile
from path_mapper import PathMapper, PathMapping, MappingStatus, MappingStatistics
from android_path_mapper import AndroidPathMapper


class PasswordDialog(simpledialog.Dialog):
    """Custom dialog for password entry."""

    def __init__(self, parent, title="Enter Password"):
        self.password = None
        super().__init__(parent, title)

    def body(self, master):
        ttk.Label(master, text="Backup is encrypted. Enter password:").grid(row=0, column=0, columnspan=2, pady=5)
        self.password_entry = ttk.Entry(master, show="*", width=40)
        self.password_entry.grid(row=1, column=0, columnspan=2, padx=5, pady=5)
        return self.password_entry

    def apply(self):
        self.password = self.password_entry.get()


class StatusBar(ttk.Frame):
    """Status bar at bottom of window with optional progress bar."""

    def __init__(self, parent):
        super().__init__(parent)

        # Status label
        self.status_var = tk.StringVar(value="Ready")
        self.label = ttk.Label(self, textvariable=self.status_var, anchor=tk.W)
        self.label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # Progress bar (hidden by default)
        self.progress = ttk.Progressbar(self, mode='determinate', length=200)
        self._progress_visible = False

    def set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def show_progress(self, maximum: int = 100):
        """Show the progress bar and set its maximum value."""
        if not self._progress_visible:
            self.progress.pack(side=tk.RIGHT, padx=5)
            self._progress_visible = True
        self.progress.configure(maximum=maximum, value=0)
        self.update_idletasks()

    def set_progress(self, value: int, maximum: int = None):
        """Update progress bar value."""
        if maximum is not None:
            self.progress.configure(maximum=maximum)
        self.progress.configure(value=value)
        self.update_idletasks()

    def hide_progress(self):
        """Hide the progress bar."""
        if self._progress_visible:
            self.progress.pack_forget()
            self._progress_visible = False
            self.update_idletasks()


class StatisticsPanel(ttk.LabelFrame):
    """Panel showing mapping statistics."""

    def __init__(self, parent, on_view_parsing_log=None):
        super().__init__(parent, text="Statistics", padding=10)
        self.labels: Dict[str, ttk.Label] = {}
        self.on_view_parsing_log = on_view_parsing_log
        self._parsing_log = None
        self._create_widgets()

    def _create_widgets(self):
        # Create a grid of statistics labels
        self.stats_frame = ttk.Frame(self)
        self.stats_frame.pack(fill=tk.BOTH, expand=True)

        # Initial placeholder text
        self.placeholder = ttk.Label(
            self.stats_frame,
            text="Load both a backup and filesystem acquisition to see statistics",
            foreground="gray"
        )
        self.placeholder.pack(pady=20)

    def update_statistics(self, stats: MappingStatistics, parsing_log=None):
        """Update the statistics display."""
        self._parsing_log = parsing_log

        # Clear existing content
        for widget in self.stats_frame.winfo_children():
            widget.destroy()

        # Summary section
        summary_frame = ttk.LabelFrame(self.stats_frame, text="Summary", padding=5)
        summary_frame.pack(fill=tk.X, padx=5, pady=5)

        row = 0

        # Build summary data with parsing log breakdown if available
        summary_data = [
            ("Manifest.db Rows:", str(stats.manifest_db_row_count)),
        ]

        # Add parsing log breakdown if available
        if parsing_log:
            summary_data.extend([
                ("  → Files:", str(parsing_log.files_added)),
                ("  → Directories:", str(parsing_log.directories_added)),
            ])
            # Show size verification stats if we have actual sizes
            if parsing_log.size_mismatches > 0 or parsing_log.manifest_size_zero > 0:
                summary_data.extend([
                    ("  → Size mismatches:", str(parsing_log.size_mismatches)),
                    ("  → Manifest size=0:", str(parsing_log.manifest_size_zero)),
                ])

        summary_data.extend([
            ("", ""),
            ("Backup Files:", str(stats.total_backup_files)),
            ("Backup Directories:", str(stats.total_backup_directories)),
            ("Filesystem Files:", str(stats.total_filesystem_files)),
            ("Filesystem Directories:", str(stats.total_filesystem_directories)),
            ("", ""),
            ("Successfully Mapped:", f"{stats.mapped_files} ({stats.mapped_files / max(1, stats.total_backup_files) * 100:.1f}%)"),
            ("Not Found in FS:", f"{stats.not_found_files} ({stats.not_found_files / max(1, stats.total_backup_files) * 100:.1f}%)"),
            ("Unmappable:", f"{stats.unmappable_files} ({stats.unmappable_files / max(1, stats.total_backup_files) * 100:.1f}%)"),
            ("", ""),
            ("Files only in Backup:", str(stats.backup_only_files)),
            ("Files only in Filesystem:", str(stats.filesystem_only_files)),
            ("", ""),
            ("Backup Coverage of FS:", f"{stats.backup_coverage_percent:.1f}%"),
        ])

        for label_text, value_text in summary_data:
            if label_text == "":
                ttk.Separator(summary_frame, orient=tk.HORIZONTAL).grid(
                    row=row, column=0, columnspan=2, sticky='ew', pady=3
                )
            else:
                ttk.Label(summary_frame, text=label_text).grid(row=row, column=0, sticky='w', padx=5)
                ttk.Label(summary_frame, text=value_text).grid(row=row, column=1, sticky='e', padx=5)
            row += 1

        # Add button to view parsing log
        if parsing_log:
            btn_frame = ttk.Frame(self.stats_frame)
            btn_frame.pack(fill=tk.X, padx=5, pady=10)
            ttk.Button(
                btn_frame,
                text="View Parsing Log",
                command=self._show_parsing_log
            ).pack(side=tk.LEFT)

    def _show_parsing_log(self):
        """Show the parsing log in a new window."""
        if not self._parsing_log:
            return

        if self.on_view_parsing_log:
            self.on_view_parsing_log(self._parsing_log)


class BackupTreeView(ttk.Frame):
    """Tree view for iOS backup files organized by domain."""

    def __init__(self, parent, on_select_callback=None, on_extract_callback=None):
        super().__init__(parent)
        self.on_select_callback = on_select_callback
        self.on_extract_callback = on_extract_callback
        self.backup: Optional[iOSBackup] = None
        self.file_nodes: Dict[str, BackupFile] = {}  # node_id -> BackupFile
        self._all_items: List[Tuple[str, str, BackupFile]] = []  # (node_id, display_path, BackupFile)
        self._unmapped_files: set = set()  # Set of BackupFile objects that are unmapped
        self._programmatic_selection: bool = False  # Flag to prevent callback during programmatic selection
        self._create_widgets()

    def _create_widgets(self):
        # Header (dynamic - updated when backup loaded)
        self.header_var = tk.StringVar(value="Backup")
        header = ttk.Label(self, textvariable=self.header_var, font=('TkDefaultFont', 10, 'bold'))
        header.pack(pady=5)

        # Info label
        self.info_var = tk.StringVar(value="No backup loaded")
        self.info_label = ttk.Label(self, textvariable=self.info_var, foreground="gray")
        self.info_label.pack()

        # Filter frame
        filter_frame = ttk.Frame(self)
        filter_frame.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(filter_frame, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add('write', self._on_filter_change)
        self.filter_entry = ttk.Entry(filter_frame, textvariable=self.filter_var)
        self.filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # Unmapped only checkbox
        self.unmapped_only_var = tk.BooleanVar(value=False)
        self.unmapped_checkbox = ttk.Checkbutton(
            filter_frame, text="Unmapped only",
            variable=self.unmapped_only_var,
            command=self._on_filter_change
        )
        self.unmapped_checkbox.pack(side=tk.LEFT, padx=5)

        self.filter_count_var = tk.StringVar(value="")
        ttk.Label(filter_frame, textvariable=self.filter_count_var, foreground="gray").pack(side=tk.RIGHT)

        # Treeview with scrollbars
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.tree = ttk.Treeview(tree_frame, selectmode='browse')
        self.tree.heading('#0', text='Backup Files', anchor='w')

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')

        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        # Bind selection event
        self.tree.bind('<<TreeviewSelect>>', self._on_select)

        # Action buttons frame
        action_frame = ttk.Frame(self)
        action_frame.pack(fill=tk.X, padx=5, pady=2)

        self.extract_btn = ttk.Button(
            action_frame, text="Extract Selected",
            command=self._on_extract_click
        )
        self.extract_btn.pack(side=tk.LEFT, padx=2)
        self.extract_btn.configure(state='disabled')

    def _on_extract_click(self):
        """Handle extract button click."""
        selected = self.get_selected_file()
        if selected and self.on_extract_callback:
            self.on_extract_callback(selected)

    def _on_filter_change(self, *args):
        """Handle filter text changes."""
        self._apply_filter()

    def _on_select(self, event):
        selection = self.tree.selection()
        if selection:
            node_id = selection[0]
            backup_file = self.file_nodes.get(node_id)
            if backup_file:
                self.extract_btn.configure(state='normal')
                # Only call callback if this is a user-initiated selection
                if self.on_select_callback and not self._programmatic_selection:
                    self.on_select_callback(backup_file)
            else:
                self.extract_btn.configure(state='disabled')
        else:
            self.extract_btn.configure(state='disabled')

    def load_backup(self, backup):
        """Load and display a backup in the tree."""
        self.backup = backup
        self.file_nodes.clear()
        self._all_items.clear()
        self.filter_var.set("")  # Clear filter

        # Clear existing tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Update header based on backup type
        if hasattr(backup, 'backup_type') and backup.backup_type == 'android':
            self.header_var.set("Android Backup")
        else:
            self.header_var.set("iOS Backup")

        # Update info label
        if hasattr(backup, 'backup_type') and backup.backup_type == 'android':
            info_text = f"{backup.device_name or 'Android Device'}"
            if backup.android_version:
                info_text += f" ({backup.android_version})"
        else:
            info_text = f"{backup.device_name or 'Unknown Device'}"
        if backup.ios_version:
            info_text += f" (iOS {backup.ios_version})"
        if backup.is_encrypted:
            info_text += " [Encrypted]"
        self.info_var.set(info_text)

        # Store all files for filtering
        for bf in backup.files:
            if not bf.is_directory:
                display_path = bf.full_domain_path
                self._all_items.append((None, display_path, bf))

        # Build the tree
        self._build_tree(backup.files)

        # Update filter count
        self.filter_count_var.set(f"{len(self._all_items)} files")

    def set_unmapped_files(self, unmapped_files: List[BackupFile]):
        """Set the list of unmapped files for filtering."""
        self._unmapped_files = set(id(bf) for bf in unmapped_files)

    def _build_tree(self, files: List[BackupFile], filter_text: str = ""):
        """Build or rebuild the tree, optionally filtered."""
        self.file_nodes.clear()

        # Clear existing tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.backup:
            return

        # Filter files if needed
        filter_lower = filter_text.lower()
        unmapped_only = self.unmapped_only_var.get()

        filtered_files = []
        for bf in files:
            if bf.is_directory:
                continue
            # Apply text filter
            if filter_lower and filter_lower not in bf.full_domain_path.lower():
                continue
            # Apply unmapped filter
            if unmapped_only and id(bf) not in self._unmapped_files:
                continue
            filtered_files.append(bf)

        # Group by domain
        files_by_domain: Dict[str, List[BackupFile]] = {}
        for bf in filtered_files:
            if bf.domain not in files_by_domain:
                files_by_domain[bf.domain] = []
            files_by_domain[bf.domain].append(bf)

        # Sort domains
        sorted_domains = sorted(files_by_domain.keys())

        for domain in sorted_domains:
            domain_files = files_by_domain[domain]

            # Create domain node
            domain_node = self.tree.insert('', 'end', text=f"{domain} ({len(domain_files)} files)", open=bool(filter_lower))

            # Build directory structure within domain
            dir_tree: Dict[str, str] = {}  # path -> node_id

            for bf in sorted(domain_files, key=lambda f: f.relative_path):
                path_parts = bf.relative_path.split('/') if bf.relative_path else []

                # Create intermediate directories
                current_path = ""
                parent_node = domain_node

                for i, part in enumerate(path_parts[:-1]):
                    current_path = f"{current_path}/{part}" if current_path else part
                    if current_path not in dir_tree:
                        dir_node = self.tree.insert(parent_node, 'end', text=part + "/", open=bool(filter_lower))
                        dir_tree[current_path] = dir_node
                    parent_node = dir_tree[current_path]

                # Add file node
                filename = path_parts[-1] if path_parts else bf.relative_path or "(root)"
                file_node = self.tree.insert(parent_node, 'end', text=filename)
                self.file_nodes[file_node] = bf

        # Update filter count
        if filter_lower:
            self.filter_count_var.set(f"{len(filtered_files)} / {len(self._all_items)} files")
        else:
            self.filter_count_var.set(f"{len(self._all_items)} files")

    def _apply_filter(self):
        """Apply the current filter to the tree."""
        if not self.backup:
            return
        filter_text = self.filter_var.get()
        self._build_tree(self.backup.files, filter_text)

    def get_selected_file(self) -> Optional[BackupFile]:
        """Get the currently selected backup file."""
        selection = self.tree.selection()
        if selection:
            return self.file_nodes.get(selection[0])
        return None

    def select_file(self, backup_file: BackupFile) -> bool:
        """
        Select a backup file in the tree programmatically.

        Args:
            backup_file: The backup file to select

        Returns:
            True if file was found and selected, False otherwise
        """
        # Find the node for this backup file
        for node_id, bf in self.file_nodes.items():
            if bf == backup_file:
                # Expand parents
                parent = self.tree.parent(node_id)
                while parent:
                    self.tree.item(parent, open=True)
                    parent = self.tree.parent(parent)
                # Unbind event to prevent callback loop, rebind after events processed
                self.tree.unbind('<<TreeviewSelect>>')
                self.tree.selection_set(node_id)
                self.tree.see(node_id)
                # Rebind after pending events are processed (not immediately!)
                self.after(10, lambda: self.tree.bind('<<TreeviewSelect>>', self._on_select))
                self.extract_btn.configure(state='normal')
                return True
        return False

    def clear(self):
        """Clear the tree view."""
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.file_nodes.clear()
        self.backup = None
        self.info_var.set("No backup loaded")


class FilesystemTreeView(ttk.Frame):
    """Tree view for filesystem acquisition."""

    def __init__(self, parent, on_select_callback=None, on_extract_callback=None):
        super().__init__(parent)
        self.on_select_callback = on_select_callback
        self.on_extract_callback = on_extract_callback
        self.filesystem: Optional[FilesystemAcquisition] = None
        self.file_nodes: Dict[str, FilesystemFile] = {}  # node_id -> FilesystemFile
        self.path_to_node: Dict[str, str] = {}  # normalized_path -> node_id
        self._all_files: List[FilesystemFile] = []  # All non-directory files for filtering
        self._total_file_count: int = 0
        self._programmatic_selection: bool = False  # Flag to prevent callback during programmatic selection
        self._create_widgets()

    def _create_widgets(self):
        # Header
        header = ttk.Label(self, text="Filesystem Acquisition", font=('TkDefaultFont', 10, 'bold'))
        header.pack(pady=5)

        # Info label
        self.info_var = tk.StringVar(value="No filesystem loaded")
        self.info_label = ttk.Label(self, textvariable=self.info_var, foreground="gray")
        self.info_label.pack()

        # Filter frame
        filter_frame = ttk.Frame(self)
        filter_frame.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(filter_frame, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add('write', self._on_filter_change)
        self.filter_entry = ttk.Entry(filter_frame, textvariable=self.filter_var)
        self.filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.filter_count_var = tk.StringVar(value="")
        ttk.Label(filter_frame, textvariable=self.filter_count_var, foreground="gray").pack(side=tk.RIGHT)

        # Treeview with scrollbars
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.tree = ttk.Treeview(tree_frame, selectmode='browse')
        self.tree.heading('#0', text='Filesystem Files', anchor='w')

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')

        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        # Configure tags for highlighting
        self.tree.tag_configure('highlight', background='#90EE90')  # Light green
        self.tree.tag_configure('not_found', background='#FFB6C1')  # Light red

        # Bind selection event
        self.tree.bind('<<TreeviewSelect>>', self._on_select)

        # Action buttons frame
        action_frame = ttk.Frame(self)
        action_frame.pack(fill=tk.X, padx=5, pady=2)

        self.extract_btn = ttk.Button(
            action_frame, text="Extract Selected",
            command=self._on_extract_click
        )
        self.extract_btn.pack(side=tk.LEFT, padx=2)
        self.extract_btn.configure(state='disabled')

    def _on_select(self, event):
        """Handle selection in the tree."""
        selected = self.get_selected_file()
        if selected:
            self.extract_btn.configure(state='normal')
            # Only call callback if this is a user-initiated selection
            if self.on_select_callback and not self._programmatic_selection:
                self.on_select_callback(selected)
        else:
            self.extract_btn.configure(state='disabled')

    def _on_extract_click(self):
        """Handle extract button click."""
        selected = self.get_selected_file()
        if selected and self.on_extract_callback:
            self.on_extract_callback(selected)

    def get_selected_file(self) -> Optional[FilesystemFile]:
        """Get the currently selected filesystem file."""
        selection = self.tree.selection()
        if selection:
            return self.file_nodes.get(selection[0])
        return None

    def load_filesystem(self, filesystem: FilesystemAcquisition):
        """Load and display a filesystem acquisition in the tree."""
        self.filesystem = filesystem
        self.file_nodes.clear()
        self.path_to_node.clear()
        self._all_files = [f for f in filesystem.files if not f.is_directory]
        self._total_file_count = len(self._all_files)
        self.filter_var.set("")

        # Update info label
        self.info_var.set(f"{filesystem.format.upper()} - {self._total_file_count} files")

        # Build the tree
        self._build_tree()

        # Update filter count
        self.filter_count_var.set(f"{self._total_file_count} files")

    def _on_filter_change(self, *args):
        """Handle filter text changes."""
        self._build_tree()

    def _build_tree(self, filter_text: str = None):
        """Build or rebuild the tree, optionally filtered."""
        if filter_text is None:
            filter_text = self.filter_var.get()

        self.file_nodes.clear()
        self.path_to_node.clear()

        # Clear existing tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.filesystem:
            return

        # Filter files if needed
        filter_lower = filter_text.strip().lower()
        if filter_lower:
            filtered_files = [f for f in self._all_files if filter_lower in f.path.lower()]
        else:
            filtered_files = self._all_files

        # Build directory tree structure
        dir_nodes: Dict[str, str] = {}  # path -> node_id

        # Sort files by path for hierarchical display
        sorted_files = sorted(filtered_files, key=lambda f: f.path)

        for ff in sorted_files:
            path = ff.normalized_path
            path_parts = [p for p in path.split('/') if p]

            # Create intermediate directories
            current_path = ""
            parent_node = ''

            for i, part in enumerate(path_parts[:-1]):
                current_path = f"/{'/'.join(path_parts[:i+1])}"

                if current_path not in dir_nodes:
                    dir_node = self.tree.insert(parent_node, 'end', text=part + "/", open=bool(filter_lower))
                    dir_nodes[current_path] = dir_node

                parent_node = dir_nodes[current_path]

            # Add file node
            filename = path_parts[-1] if path_parts else path
            file_node = self.tree.insert(parent_node, 'end', text=filename)
            self.file_nodes[file_node] = ff
            self.path_to_node[ff.normalized_path] = file_node

            # Also index alternate paths
            if ff.normalized_path.startswith('/private/'):
                alt_path = ff.normalized_path[8:]
                self.path_to_node[alt_path] = file_node

        # Update filter count
        if filter_lower:
            self.filter_count_var.set(f"{len(filtered_files)} / {self._total_file_count} files")
        else:
            self.filter_count_var.set(f"{self._total_file_count} files")

    def highlight_path(self, path: Optional[str], mapping_status: MappingStatus = MappingStatus.MAPPED):
        """
        Highlight a path in the filesystem tree.

        Args:
            path: The filesystem path to highlight
            mapping_status: The mapping status to determine highlighting style
        """
        # Clear all highlights first
        for node_id in self.file_nodes.keys():
            self.tree.item(node_id, tags=())

        if path is None:
            return

        # Find the node for this path
        node_id = self.path_to_node.get(path)

        if node_id is None:
            # Try with /private prefix
            if not path.startswith('/private/'):
                alt_path = '/private' + path if path.startswith('/') else '/private/' + path
                node_id = self.path_to_node.get(alt_path)

        if node_id:
            # Apply highlight tag
            if mapping_status == MappingStatus.MAPPED:
                self.tree.item(node_id, tags=('highlight',))
            else:
                self.tree.item(node_id, tags=('not_found',))

            # Expand parents and scroll to node
            self._expand_to_node(node_id)
            self.tree.see(node_id)
            # Unbind event to prevent callback loop, rebind after events processed
            self.tree.unbind('<<TreeviewSelect>>')
            self.tree.selection_set(node_id)
            # Rebind after pending events are processed (not immediately!)
            self.after(10, lambda: self.tree.bind('<<TreeviewSelect>>', self._on_select))

    def _expand_to_node(self, node_id: str):
        """Expand all parent nodes to make a node visible."""
        parent = self.tree.parent(node_id)
        while parent:
            self.tree.item(parent, open=True)
            parent = self.tree.parent(parent)

    def clear(self):
        """Clear the tree view."""
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.file_nodes.clear()
        self.path_to_node.clear()
        self._all_files = []
        self._total_file_count = 0
        self.filesystem = None
        self.info_var.set("No filesystem loaded")
        self.filter_var.set("")
        self.filter_count_var.set("")


class MappingInfoPanel(ttk.LabelFrame):
    """Panel showing information about the current mapping."""

    def __init__(self, parent, on_compare_hashes=None):
        super().__init__(parent, text="Mapping Details", padding=10)
        self.on_compare_hashes = on_compare_hashes
        self.current_mapping: Optional[PathMapping] = None
        self._create_widgets()

    def _create_widgets(self):
        self.backup_path_var = tk.StringVar(value="")
        self.fs_path_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="")
        self.size_var = tk.StringVar(value="")
        self.notes_var = tk.StringVar(value="")
        self.hash_result_var = tk.StringVar(value="")

        ttk.Label(self, text="Backup Path:").grid(row=0, column=0, sticky='w', padx=5)
        ttk.Label(self, textvariable=self.backup_path_var, wraplength=400).grid(row=0, column=1, columnspan=2, sticky='w', padx=5)

        ttk.Label(self, text="Filesystem Path:").grid(row=1, column=0, sticky='w', padx=5)
        ttk.Label(self, textvariable=self.fs_path_var, wraplength=400).grid(row=1, column=1, columnspan=2, sticky='w', padx=5)

        ttk.Label(self, text="Status:").grid(row=2, column=0, sticky='w', padx=5)
        self.status_label = ttk.Label(self, textvariable=self.status_var)
        self.status_label.grid(row=2, column=1, columnspan=2, sticky='w', padx=5)

        ttk.Label(self, text="Size:").grid(row=3, column=0, sticky='w', padx=5)
        self.size_label = ttk.Label(self, textvariable=self.size_var)
        self.size_label.grid(row=3, column=1, columnspan=2, sticky='w', padx=5)

        ttk.Label(self, text="Notes:").grid(row=4, column=0, sticky='w', padx=5)
        ttk.Label(self, textvariable=self.notes_var, wraplength=400, foreground='gray').grid(row=4, column=1, columnspan=2, sticky='w', padx=5)

        # Hash comparison result
        ttk.Label(self, text="Hash:").grid(row=5, column=0, sticky='w', padx=5)
        self.hash_label = ttk.Label(self, textvariable=self.hash_result_var, wraplength=400)
        self.hash_label.grid(row=5, column=1, columnspan=2, sticky='w', padx=5)

        # Action buttons
        button_frame = ttk.Frame(self)
        button_frame.grid(row=6, column=0, columnspan=3, sticky='w', pady=10)

        self.compare_btn = ttk.Button(button_frame, text="Compare Hashes", command=self._on_compare_click)
        self.compare_btn.pack(side=tk.LEFT, padx=5)
        self.compare_btn.configure(state='disabled')

    def _on_compare_click(self):
        if self.on_compare_hashes and self.current_mapping:
            self.on_compare_hashes(self.current_mapping)

    def _format_size(self, size: int) -> str:
        """Format file size in human-readable format."""
        if size == 0:
            return "0 B"
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != 'B' else f"{size} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def update_mapping(self, mapping: Optional[PathMapping]):
        """Update the display with mapping information."""
        self.current_mapping = mapping
        self.hash_result_var.set("")

        if mapping is None:
            self.backup_path_var.set("")
            self.fs_path_var.set("")
            self.status_var.set("")
            self.size_var.set("")
            self.notes_var.set("")
            self.compare_btn.configure(state='disabled')
            self.size_label.configure(foreground='black')
            return

        self.backup_path_var.set(mapping.backup_file.full_domain_path)
        self.fs_path_var.set(mapping.filesystem_path or "N/A")
        self.status_var.set(mapping.status.value.replace('_', ' ').title())
        self.notes_var.set(mapping.notes or "")

        # Display file sizes - prefer actual_file_size over manifest size
        manifest_size = mapping.backup_file.file_size
        actual_backup_size = mapping.backup_file.actual_file_size
        fs_size = mapping.filesystem_file.size if mapping.filesystem_file else None

        # Use actual size if available, otherwise manifest size
        backup_size_to_compare = actual_backup_size if actual_backup_size is not None else manifest_size

        # Build size text
        if actual_backup_size is not None and manifest_size != actual_backup_size:
            # Show both manifest and actual if they differ
            backup_text = f"Backup: {self._format_size(actual_backup_size)} (manifest: {self._format_size(manifest_size)})"
        else:
            backup_text = f"Backup: {self._format_size(backup_size_to_compare)}"

        if fs_size is not None:
            size_text = f"{backup_text} | Filesystem: {self._format_size(fs_size)}"
            if backup_size_to_compare == fs_size:
                self.size_label.configure(foreground='green')
                size_text += " ✓"
            elif actual_backup_size is not None and actual_backup_size == fs_size:
                # Actual matches filesystem even if manifest was wrong
                self.size_label.configure(foreground='green')
                size_text += " ✓"
            else:
                self.size_label.configure(foreground='orange')
                size_text += " ⚠️ MISMATCH"
        else:
            size_text = backup_text
            self.size_label.configure(foreground='black')

        self.size_var.set(size_text)

        # Color code status
        if mapping.status == MappingStatus.MAPPED:
            self.status_label.configure(foreground='green')
            self.compare_btn.configure(state='normal')
        elif mapping.status == MappingStatus.NOT_FOUND:
            self.status_label.configure(foreground='orange')
            self.compare_btn.configure(state='disabled')
        else:
            self.status_label.configure(foreground='red')
            self.compare_btn.configure(state='disabled')

    def set_hash_result(self, result: str, match: Optional[bool] = None):
        """Set the hash comparison result."""
        self.hash_result_var.set(result)
        if match is True:
            self.hash_label.configure(foreground='green')
        elif match is False:
            self.hash_label.configure(foreground='red')
        else:
            self.hash_label.configure(foreground='gray')


class MainApplication(tk.Tk):
    """Main application window."""

    def __init__(self):
        super().__init__()

        self.title("Mobile Backup to Filesystem Comparison Tool")
        self.geometry("1400x900")

        # Data
        self.backup = None  # iOSBackup or AndroidBackup
        self.backup_type: Optional[str] = None  # 'ios' or 'android'
        self._backup_parser = None  # Store parser for content extraction (Android)
        self.filesystem: Optional[FilesystemAcquisition] = None
        self.mapper = None  # PathMapper or AndroidPathMapper
        self._selecting: bool = False  # Flag to prevent recursive selection

        self._create_menu()
        self._create_widgets()
        self._configure_layout()

    def _create_menu(self):
        """Create the menu bar."""
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)

        # Backup submenu
        backup_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Load Backup", menu=backup_menu)
        backup_menu.add_command(label="From Folder...", command=self._load_backup_folder)
        backup_menu.add_command(label="From File...", command=self._load_backup_file)

        # Filesystem submenu
        fs_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Load Filesystem", menu=fs_menu)
        fs_menu.add_command(label="From Folder...", command=self._load_filesystem_folder)
        fs_menu.add_command(label="From File...", command=self._load_filesystem_file)

        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)

        # Export menu
        export_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Export", menu=export_menu)
        export_menu.add_command(label="Export Statistics...", command=self._export_statistics)
        export_menu.add_command(label="Export Unmapped Files List...", command=self._export_unmapped_list)
        export_menu.add_command(label="Export Full Mapping Report (CSV)...", command=self._export_full_report)

        # View menu
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Expand All Backup", command=self._expand_backup_tree)
        view_menu.add_command(label="Collapse All Backup", command=self._collapse_backup_tree)
        view_menu.add_separator()
        view_menu.add_command(label="Expand All Filesystem", command=self._expand_fs_tree)
        view_menu.add_command(label="Collapse All Filesystem", command=self._collapse_fs_tree)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=self._show_about)

    def _create_widgets(self):
        """Create the main widgets."""
        # Main container
        self.main_frame = ttk.Frame(self)

        # Top toolbar
        self.toolbar = ttk.Frame(self.main_frame)

        # Backup loading section (iOS or Android)
        backup_frame = ttk.LabelFrame(self.toolbar, text="Load Backup", padding=3)
        backup_frame.pack(side=tk.LEFT, padx=5)
        ttk.Button(backup_frame, text="From Folder", command=self._load_backup_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(backup_frame, text="From File", command=self._load_backup_file).pack(side=tk.LEFT, padx=2)

        # Filesystem loading section
        fs_frame = ttk.LabelFrame(self.toolbar, text="Load Filesystem", padding=3)
        fs_frame.pack(side=tk.LEFT, padx=5)
        ttk.Button(fs_frame, text="From Folder", command=self._load_filesystem_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(fs_frame, text="From File", command=self._load_filesystem_file).pack(side=tk.LEFT, padx=2)

        ttk.Separator(self.toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Button(self.toolbar, text="Re-run Mapping", command=self._run_mapping).pack(side=tk.LEFT, padx=5)
        ttk.Button(self.toolbar, text="Export Stats", command=self._export_statistics).pack(side=tk.LEFT, padx=5)

        # Paned window for tree views and info panel
        self.paned = ttk.PanedWindow(self.main_frame, orient=tk.HORIZONTAL)

        # Left panel - Backup tree
        self.backup_tree = BackupTreeView(
            self.paned,
            on_select_callback=self._on_backup_select,
            on_extract_callback=self._extract_backup_file
        )

        # Right panel - Filesystem tree
        self.fs_tree = FilesystemTreeView(
            self.paned,
            on_select_callback=self._on_filesystem_select,
            on_extract_callback=self._extract_filesystem_file
        )

        # Bottom section with mapping info and stats
        self.bottom_frame = ttk.Frame(self.main_frame)

        # Mapping info panel
        self.mapping_info = MappingInfoPanel(
            self.bottom_frame,
            on_compare_hashes=self._compare_hashes
        )

        # Statistics panel
        self.stats_panel = StatisticsPanel(self.bottom_frame, on_view_parsing_log=self._show_parsing_log)

        # Status bar
        self.status_bar = StatusBar(self.main_frame)

    def _configure_layout(self):
        """Configure the layout."""
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.toolbar.pack(fill=tk.X, pady=5)

        self.paned.pack(fill=tk.BOTH, expand=True, pady=5)
        self.paned.add(self.backup_tree, weight=1)
        self.paned.add(self.fs_tree, weight=1)

        self.bottom_frame.pack(fill=tk.X, pady=5)
        self.mapping_info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        self.stats_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5)

        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _load_backup_folder(self):
        """Load an iOS backup from a folder."""
        path = filedialog.askdirectory(title="Select iOS Backup Directory")
        if path:
            self._load_backup_from_path(path)

    def _load_backup_file(self):
        """Load a backup from a file (iOS ZIP or Android .ab)."""
        path = filedialog.askopenfilename(
            title="Select Backup File",
            filetypes=[
                ("Backup files", "*.zip *.ab"),
                ("ZIP files", "*.zip"),
                ("Android backups", "*.ab"),
                ("All files", "*.*")
            ]
        )
        if path:
            self._load_backup_from_path(path)

    def _load_backup_from_path(self, path: str):
        """Load a backup from the given path (auto-detects iOS vs Android)."""
        if iOSBackupParser.is_ios_backup(path):
            self.backup_type = 'ios'
            self._load_ios_backup(path)
        elif AndroidBackupParser.is_android_backup(path):
            self.backup_type = 'android'
            self._load_android_backup(path)
        else:
            messagebox.showerror(
                "Error",
                "Selected path is not a recognized backup format.\n\n"
                "Supported formats:\n"
                "- iOS backup (directory with Manifest.db or ZIP)\n"
                "- Android backup (.ab file)"
            )

    def _load_android_backup(self, path: str):
        """Load an Android backup from the given path."""
        self.status_bar.set_status(f"Loading Android backup from {path}...")
        self.status_bar.progress.configure(mode='determinate')
        self.status_bar.show_progress()
        self.status_bar.progress['value'] = 0
        self.update_idletasks()

        try:
            parser = AndroidBackupParser(path)

            def password_callback():
                dialog = PasswordDialog(self, "Enter Backup Password")
                return dialog.password

            def progress_callback(current, total, message):
                if total > 0:
                    self.status_bar.progress['value'] = current
                self.status_bar.set_status(message)
                self.update_idletasks()

            self.backup = parser.parse(
                password_callback=password_callback,
                progress_callback=progress_callback
            )
            self._backup_parser = parser

            self.status_bar.set_status("Building backup tree...")
            self.status_bar.progress['value'] = 95
            self.update_idletasks()

            self.backup_tree.load_backup(self.backup)

            file_count = len([f for f in self.backup.files if not f.is_directory])
            self.status_bar.progress['value'] = 100
            self.status_bar.hide_progress()
            self.status_bar.set_status(f"Loaded Android backup: {file_count} files")

            if self.filesystem:
                self._run_mapping()

        except Exception as e:
            self.status_bar.progress.configure(mode='determinate')
            self.status_bar.hide_progress()
            messagebox.showerror("Error", f"Failed to load Android backup: {e}")
            self.status_bar.set_status("Failed to load backup")

    def _load_ios_backup(self, path: str):
        """Load an iOS backup from the given path."""
        self.status_bar.set_status(f"Loading backup from {path}...")
        self.status_bar.progress.configure(mode='determinate')
        self.status_bar.show_progress()
        self.status_bar.progress['value'] = 0
        self.update_idletasks()

        try:
            parser = iOSBackupParser(path)
            self._backup_parser = parser

            def password_callback():
                dialog = PasswordDialog(self, "Enter Backup Password")
                return dialog.password

            def progress_callback(current, total, message):
                # Scale progress: manifest parsing 0-30%, file sizes 30-90%, tree building 90-100%
                if "manifest" in message.lower():
                    self.status_bar.progress['value'] = 10
                elif "file sizes" in message.lower() or "Reading" in message:
                    # Scale the file size reading progress (30-90%)
                    if total > 0:
                        pct = 30 + (current / total) * 60
                    else:
                        pct = 30
                    self.status_bar.progress['value'] = pct
                elif "complete" in message.lower():
                    self.status_bar.progress['value'] = 90
                self.status_bar.set_status(message)
                self.update_idletasks()

            self.backup = parser.parse(
                password_callback=password_callback,
                progress_callback=progress_callback
            )

            self.status_bar.set_status("Building backup tree...")
            self.status_bar.progress['value'] = 95
            self.update_idletasks()

            self.backup_tree.load_backup(self.backup)

            self.status_bar.progress['value'] = 100
            self.status_bar.hide_progress()
            self.status_bar.set_status(f"Loaded backup: {len(self.backup.files)} files")

            # Run mapping if filesystem is also loaded
            if self.filesystem:
                self._run_mapping()

        except ValueError as e:
            self.status_bar.progress.stop()
            self.status_bar.progress.configure(mode='determinate')
            self.status_bar.hide_progress()
            messagebox.showerror("Error", str(e))
            self.status_bar.set_status("Failed to load backup")
        except Exception as e:
            self.status_bar.progress.stop()
            self.status_bar.progress.configure(mode='determinate')
            self.status_bar.hide_progress()
            messagebox.showerror("Error", f"Failed to load backup: {e}")
            self.status_bar.set_status("Failed to load backup")

    def _load_filesystem_folder(self):
        """Load a filesystem acquisition from a folder."""
        path = filedialog.askdirectory(title="Select Filesystem Directory")
        if path:
            self._load_filesystem_from_path(path)

    def _load_filesystem_file(self):
        """Load a filesystem acquisition from a TAR/ZIP file."""
        path = filedialog.askopenfilename(
            title="Select Filesystem Archive",
            filetypes=[
                ("Archive files", "*.tar *.tar.gz *.tgz *.zip"),
                ("TAR files", "*.tar *.tar.gz *.tgz"),
                ("ZIP files", "*.zip"),
                ("All files", "*.*")
            ]
        )
        if path:
            self._load_filesystem_from_path(path)

    def _load_filesystem_from_path(self, path: str):
        """Load a filesystem acquisition from the given path."""
        self.status_bar.set_status(f"Loading filesystem from {path}...")
        self.status_bar.show_progress(100)
        self.update_idletasks()

        def progress_callback(current, total, message):
            self.status_bar.set_status(message)
            if total > 0:
                self.status_bar.set_progress(current, total)
            self.update_idletasks()

        try:
            loader = FilesystemLoader(path, progress_callback=progress_callback)
            self.filesystem = loader.load()

            self.status_bar.set_status("Building file tree...")
            self.status_bar.set_progress(0, 100)
            self.update_idletasks()
            self.fs_tree.load_filesystem(self.filesystem)

            file_count = len([f for f in self.filesystem.files if not f.is_directory])
            self.status_bar.hide_progress()
            self.status_bar.set_status(f"Loaded filesystem: {file_count} files")

            # Show container mapping info if found
            if self.filesystem.app_container_mapping:
                self.status_bar.set_status(
                    f"Loaded filesystem: {file_count} files, "
                    f"{len(self.filesystem.app_container_mapping)} app containers resolved"
                )

            # Run mapping if backup is also loaded
            if self.backup:
                self._run_mapping()

        except Exception as e:
            self.status_bar.hide_progress()
            messagebox.showerror("Error", f"Failed to load filesystem: {e}")
            self.status_bar.set_status("Failed to load filesystem")

    def _run_mapping(self):
        """Run the path mapping between backup and filesystem."""
        if not self.backup or not self.filesystem:
            messagebox.showwarning("Warning", "Load both a backup and filesystem first")
            return

        self.status_bar.set_status("Running path mapping...")
        self.status_bar.progress.configure(mode='indeterminate')
        self.status_bar.show_progress()
        self.status_bar.progress.start(10)
        self.update_idletasks()

        try:
            if self.backup_type == 'android':
                self.mapper = AndroidPathMapper(self.backup, self.filesystem)
            else:
                self.mapper = PathMapper(self.backup, self.filesystem)
            self.mapper.map_all()

            # Update statistics (include parsing log if available)
            parsing_log = self.backup.parsing_log if self.backup else None
            self.stats_panel.update_statistics(self.mapper.statistics, parsing_log)

            # Set unmapped files for the backup tree filter
            unmapped = self.mapper.get_unmapped_backup_files()
            self.backup_tree.set_unmapped_files(unmapped)

            self.status_bar.progress.stop()
            self.status_bar.progress.configure(mode='determinate')
            self.status_bar.hide_progress()
            self.status_bar.set_status(
                f"Mapping complete: {self.mapper.statistics.mapped_files} mapped, "
                f"{self.mapper.statistics.not_found_files} not found, "
                f"{self.mapper.statistics.unmappable_files} unmappable"
            )

        except Exception as e:
            self.status_bar.progress.stop()
            self.status_bar.progress.configure(mode='determinate')
            self.status_bar.hide_progress()
            messagebox.showerror("Error", f"Mapping failed: {e}")
            self.status_bar.set_status("Mapping failed")

    def _on_backup_select(self, backup_file: BackupFile):
        """Handle selection of a backup file."""
        if self._selecting:
            return
        if not self.mapper:
            return

        self._selecting = True
        try:
            # Find the mapping for this file
            mapping = self.mapper.get_mapping_for_backup_file(backup_file)

            if mapping:
                # Update mapping info panel
                self.mapping_info.update_mapping(mapping)

                # Highlight in filesystem tree
                self.fs_tree.highlight_path(mapping.filesystem_path, mapping.status)
            else:
                self.mapping_info.update_mapping(None)
        finally:
            self._selecting = False

    def _on_filesystem_select(self, fs_file: FilesystemFile):
        """Handle selection of a filesystem file."""
        if self._selecting:
            return
        if not self.mapper:
            # No mapping available, just show basic info
            self.mapping_info.update_mapping(None)
            return

        self._selecting = True
        try:
            # Find the mapping for this filesystem file (reverse lookup)
            mapping = self.mapper.get_mapping_for_filesystem_file(fs_file)

            if mapping:
                # Update mapping info panel
                self.mapping_info.update_mapping(mapping)

                # Select the corresponding backup file in the backup tree
                self.backup_tree.select_file(mapping.backup_file)
            else:
                # File exists in filesystem but not in backup
                self.mapping_info.update_mapping(None)
                # Clear any selection highlighting in backup tree (unbind to prevent any event issues)
                self.backup_tree.tree.unbind('<<TreeviewSelect>>')
                self.backup_tree.tree.selection_set()
                # Rebind after pending events are processed
                self.after(10, lambda: self.backup_tree.tree.bind('<<TreeviewSelect>>', self.backup_tree._on_select))
        finally:
            self._selecting = False

    def _show_parsing_log(self, parsing_log):
        """Show the parsing log in a scrollable window."""
        log_window = tk.Toplevel(self)
        log_window.title("Manifest.db Parsing Log")
        log_window.geometry("900x600")

        # Create text widget with scrollbar
        frame = ttk.Frame(log_window)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        text = tk.Text(frame, wrap=tk.NONE, font=('Courier', 10))
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=text.xview)
        text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Insert log content
        text.insert(tk.END, parsing_log.to_text())
        text.configure(state=tk.DISABLED)  # Make read-only

        # Add export button
        btn_frame = ttk.Frame(log_window)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        def export_log():
            path = filedialog.asksaveasfilename(
                title="Export Parsing Log",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
            )
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(parsing_log.to_text())
                messagebox.showinfo("Export Complete", f"Parsing log exported to:\n{path}")

        ttk.Button(btn_frame, text="Export Log", command=export_log).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Close", command=log_window.destroy).pack(side=tk.RIGHT)

    def _export_statistics(self):
        """Export statistics to a file."""
        if not self.mapper:
            messagebox.showwarning("Warning", "No mapping results to export")
            return

        path = filedialog.asksaveasfilename(
            title="Export Statistics",
            defaultextension=".txt",
            filetypes=[
                ("Text files", "*.txt"),
                ("CSV files", "*.csv"),
                ("All files", "*.*")
            ]
        )

        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                stats = self.mapper.statistics

                f.write("iOS Backup to Filesystem Comparison Statistics\n")
                f.write("=" * 50 + "\n\n")

                f.write("MANIFEST.DB PARSING\n")
                f.write("-" * 30 + "\n")
                f.write(f"Manifest.db Rows: {stats.manifest_db_row_count}\n")
                if self.backup and self.backup.parsing_log:
                    log = self.backup.parsing_log
                    f.write(f"  Files: {log.files_added}\n")
                    f.write(f"  Directories: {log.directories_added}\n")
                f.write("\n")

                f.write("SUMMARY\n")
                f.write("-" * 30 + "\n")
                f.write(f"Backup Files: {stats.total_backup_files}\n")
                f.write(f"Backup Directories: {stats.total_backup_directories}\n")
                f.write(f"Filesystem Files: {stats.total_filesystem_files}\n")
                f.write(f"Filesystem Directories: {stats.total_filesystem_directories}\n\n")

                f.write(f"Successfully Mapped: {stats.mapped_files}\n")
                f.write(f"Not Found in Filesystem: {stats.not_found_files}\n")
                f.write(f"Unmappable: {stats.unmappable_files}\n\n")

                f.write(f"Files only in Backup: {stats.backup_only_files}\n")
                f.write(f"Files only in Filesystem: {stats.filesystem_only_files}\n\n")

                f.write("BY DOMAIN\n")
                f.write("-" * 30 + "\n")
                f.write(f"{'Domain':<30} {'Total':>8} {'Mapped':>8} {'Not Found':>10} {'Unmappable':>12}\n")

                by_domain = self.mapper.get_mappings_by_domain()
                for domain in sorted(by_domain.keys()):
                    domain_mappings = by_domain[domain]
                    total = len(domain_mappings)
                    mapped = sum(1 for m in domain_mappings if m.status == MappingStatus.MAPPED)
                    not_found = sum(1 for m in domain_mappings if m.status == MappingStatus.NOT_FOUND)
                    unmappable = sum(1 for m in domain_mappings if m.status == MappingStatus.UNMAPPABLE)
                    f.write(
                        f"{domain:<30} {total:>8} {mapped:>8} "
                        f"{not_found:>10} {unmappable:>12}\n"
                    )

                # Write unmapped files if any
                if stats.not_found_files > 0:
                    f.write("\n\nFILES NOT FOUND IN FILESYSTEM\n")
                    f.write("-" * 30 + "\n")
                    for m in self.mapper.mappings:
                        if m.status == MappingStatus.NOT_FOUND:
                            f.write(f"Backup: {m.backup_file.full_domain_path}\n")
                            f.write(f"  Expected: {m.filesystem_path}\n\n")

            self.status_bar.set_status(f"Statistics exported to {path}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to export: {e}")

    def _export_unmapped_list(self):
        """Export list of unmapped backup files."""
        if not self.mapper:
            messagebox.showwarning("Warning", "No mapping results to export")
            return

        unmapped = self.mapper.get_unmapped_backup_files()
        if not unmapped:
            messagebox.showinfo("Info", "No unmapped files to export")
            return

        path = filedialog.asksaveasfilename(
            title="Export Unmapped Files List",
            defaultextension=".txt",
            filetypes=[
                ("Text files", "*.txt"),
                ("All files", "*.*")
            ]
        )

        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("Unmapped Backup Files\n")
                f.write("=" * 50 + "\n\n")
                f.write(f"Total unmapped files: {len(unmapped)}\n\n")

                # Group by status
                not_found = []
                unmappable = []

                for m in self.mapper.mappings:
                    if m.status == MappingStatus.NOT_FOUND:
                        not_found.append(m)
                    elif m.status == MappingStatus.UNMAPPABLE:
                        unmappable.append(m)

                if not_found:
                    f.write("FILES NOT FOUND IN FILESYSTEM\n")
                    f.write("-" * 40 + "\n")
                    for m in not_found:
                        f.write(f"{m.backup_file.full_domain_path}\n")
                        if m.filesystem_path:
                            f.write(f"  Expected path: {m.filesystem_path}\n")
                        if m.notes:
                            f.write(f"  Notes: {m.notes}\n")
                    f.write("\n")

                if unmappable:
                    f.write("UNMAPPABLE FILES (unknown domain)\n")
                    f.write("-" * 40 + "\n")
                    for m in unmappable:
                        f.write(f"{m.backup_file.full_domain_path}\n")
                        if m.notes:
                            f.write(f"  Notes: {m.notes}\n")

            self.status_bar.set_status(f"Unmapped files list exported to {path}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to export: {e}")

    def _export_full_report(self):
        """Export full mapping report as CSV."""
        if not self.mapper:
            messagebox.showwarning("Warning", "No mapping results to export")
            return

        path = filedialog.asksaveasfilename(
            title="Export Full Mapping Report",
            defaultextension=".csv",
            filetypes=[
                ("CSV files", "*.csv"),
                ("All files", "*.*")
            ]
        )

        if not path:
            return

        try:
            import csv

            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)

                # Header
                writer.writerow([
                    "Domain",
                    "Relative Path",
                    "Full Backup Path",
                    "Filesystem Path",
                    "Status",
                    "Notes",
                    "Backup File Size",
                    "Backup Modified Time"
                ])

                # Data rows
                for m in self.mapper.mappings:
                    writer.writerow([
                        m.backup_file.domain,
                        m.backup_file.relative_path,
                        m.backup_file.full_domain_path,
                        m.filesystem_path or "",
                        m.status.value,
                        m.notes or "",
                        m.backup_file.file_size,
                        m.backup_file.modified_time or ""
                    ])

            self.status_bar.set_status(f"Full mapping report exported to {path}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to export: {e}")

    def _compare_hashes(self, mapping: PathMapping):
        """Compare hashes between backup file and filesystem file."""
        if not mapping or mapping.status != MappingStatus.MAPPED:
            return

        if not mapping.filesystem_file:
            self.mapping_info.set_hash_result("No filesystem file to compare", None)
            return

        self.status_bar.set_status("Computing file hashes...")
        self.update_idletasks()

        try:
            # Get backup file content
            if self.backup_type == 'android':
                backup_content = AndroidBackupParser.get_file_content(self.backup, mapping.backup_file)
            else:
                parser = iOSBackupParser(self.backup.path)
                backup_content = parser.get_file_content(self.backup, mapping.backup_file)

            if backup_content is None:
                self.mapping_info.set_hash_result("Could not read backup file", None)
                self.status_bar.set_status("Hash comparison failed")
                return

            # Get filesystem file content
            loader = FilesystemLoader(self.filesystem.path)
            fs_content = loader.get_file_content(self.filesystem, mapping.filesystem_file)

            if fs_content is None:
                self.mapping_info.set_hash_result("Could not read filesystem file", None)
                self.status_bar.set_status("Hash comparison failed")
                return

            # Compute SHA256 hashes
            backup_hash = hashlib.sha256(backup_content).hexdigest()
            fs_hash = hashlib.sha256(fs_content).hexdigest()

            if backup_hash == fs_hash:
                self.mapping_info.set_hash_result(
                    f"MATCH - SHA256: {backup_hash[:16]}...",
                    True
                )
                self.status_bar.set_status("Hashes match")
            else:
                self.mapping_info.set_hash_result(
                    f"MISMATCH - Backup: {backup_hash[:16]}... | FS: {fs_hash[:16]}...",
                    False
                )
                self.status_bar.set_status("Hashes do not match")

        except Exception as e:
            self.mapping_info.set_hash_result(f"Error: {e}", None)
            self.status_bar.set_status("Hash comparison failed")

    def _extract_backup_file(self, backup_file: BackupFile):
        """Extract a file from the backup."""
        if not backup_file or not self.backup:
            return

        # Determine if this is a SQLite database
        is_sqlite = backup_file.relative_path.lower().endswith(('.db', '.sqlite', '.sqlite3'))

        # Check for companion files if SQLite
        companion_files = []
        if is_sqlite:
            for ext in ['-wal', '-shm', '-journal']:
                # Look for companion in backup
                companion_rel_path = backup_file.relative_path + ext
                for bf in self.backup.files:
                    if bf.domain == backup_file.domain and bf.relative_path == companion_rel_path:
                        companion_files.append((bf, ext))
                        break

        # If companions found, ask user
        export_companions = False
        if companion_files:
            companion_names = ", ".join(ext for _, ext in companion_files)
            result = messagebox.askyesnocancel(
                "SQLite Companion Files Found",
                f"This SQLite database has companion files ({companion_names}).\n\n"
                "Would you like to export them together?\n\n"
                "Yes = Export database + companions\n"
                "No = Export database only\n"
                "Cancel = Abort export"
            )
            if result is None:  # Cancel
                return
            export_companions = result

        # Ask for save location
        suggested_name = os.path.basename(backup_file.relative_path) if backup_file.relative_path else "file"
        path = filedialog.asksaveasfilename(
            title="Extract from Backup",
            initialfile=suggested_name,
            defaultextension="",
            filetypes=[("All files", "*.*")]
        )

        if not path:
            return

        try:
            # Extract main file
            if self.backup_type == 'android':
                content = AndroidBackupParser.get_file_content(self.backup, backup_file)
            else:
                parser = iOSBackupParser(self.backup.path)
                content = parser.get_file_content(self.backup, backup_file)

            if content is None:
                messagebox.showerror("Error", "Could not read file from backup")
                return

            with open(path, 'wb') as f:
                f.write(content)

            extracted_count = 1

            # Extract companion files if requested
            if export_companions and companion_files:
                for companion_bf, ext in companion_files:
                    if self.backup_type == 'android':
                        companion_content = AndroidBackupParser.get_file_content(self.backup, companion_bf)
                    else:
                        parser = iOSBackupParser(self.backup.path)
                        companion_content = parser.get_file_content(self.backup, companion_bf)
                    if companion_content:
                        companion_path = path + ext
                        with open(companion_path, 'wb') as f:
                            f.write(companion_content)
                        extracted_count += 1

            if extracted_count > 1:
                self.status_bar.set_status(f"Extracted {extracted_count} files from backup to {os.path.dirname(path)}")
            else:
                self.status_bar.set_status(f"Extracted from backup to {path}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to extract: {e}")

    def _extract_filesystem_file(self, fs_file: FilesystemFile):
        """Extract a file from the filesystem acquisition."""
        if not fs_file or not self.filesystem:
            return

        # Determine if this is a SQLite database
        is_sqlite = fs_file.path.lower().endswith(('.db', '.sqlite', '.sqlite3'))

        # Check for companion files if SQLite
        companion_files = []
        if is_sqlite:
            for ext in ['-wal', '-shm', '-journal']:
                companion_path = fs_file.path + ext
                companion = self.filesystem.find_file(companion_path)
                if companion:
                    companion_files.append((companion, ext))

        # If companions found, ask user
        export_companions = False
        if companion_files:
            companion_names = ", ".join(ext for _, ext in companion_files)
            result = messagebox.askyesnocancel(
                "SQLite Companion Files Found",
                f"This SQLite database has companion files ({companion_names}).\n\n"
                "Would you like to export them together?\n\n"
                "Yes = Export database + companions\n"
                "No = Export database only\n"
                "Cancel = Abort export"
            )
            if result is None:  # Cancel
                return
            export_companions = result

        # Ask for save location
        suggested_name = os.path.basename(fs_file.path) if fs_file.path else "file"
        path = filedialog.asksaveasfilename(
            title="Extract from Filesystem",
            initialfile=suggested_name,
            defaultextension="",
            filetypes=[("All files", "*.*")]
        )

        if not path:
            return

        try:
            # Extract main file
            loader = FilesystemLoader(self.filesystem.path)
            content = loader.get_file_content(self.filesystem, fs_file)

            if content is None:
                messagebox.showerror("Error", "Could not read file from filesystem acquisition")
                return

            with open(path, 'wb') as f:
                f.write(content)

            extracted_count = 1

            # Extract companion files if requested
            if export_companions and companion_files:
                for companion_ff, ext in companion_files:
                    companion_content = loader.get_file_content(self.filesystem, companion_ff)
                    if companion_content:
                        companion_path = path + ext
                        with open(companion_path, 'wb') as f:
                            f.write(companion_content)
                        extracted_count += 1

            if extracted_count > 1:
                self.status_bar.set_status(f"Extracted {extracted_count} files from filesystem to {os.path.dirname(path)}")
            else:
                self.status_bar.set_status(f"Extracted from filesystem to {path}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to extract: {e}")

    def _expand_backup_tree(self):
        """Expand all nodes in backup tree."""
        self._expand_tree(self.backup_tree.tree)

    def _collapse_backup_tree(self):
        """Collapse all nodes in backup tree."""
        self._collapse_tree(self.backup_tree.tree)

    def _expand_fs_tree(self):
        """Expand all nodes in filesystem tree."""
        self._expand_tree(self.fs_tree.tree)

    def _collapse_fs_tree(self):
        """Collapse all nodes in filesystem tree."""
        self._collapse_tree(self.fs_tree.tree)

    def _expand_tree(self, tree: ttk.Treeview):
        """Expand all nodes in a tree."""
        def expand(item):
            tree.item(item, open=True)
            for child in tree.get_children(item):
                expand(child)

        for item in tree.get_children():
            expand(item)

    def _collapse_tree(self, tree: ttk.Treeview):
        """Collapse all nodes in a tree."""
        def collapse(item):
            tree.item(item, open=False)
            for child in tree.get_children(item):
                collapse(child)

        for item in tree.get_children():
            collapse(item)

    def _show_about(self):
        """Show about dialog."""
        messagebox.showinfo(
            "About",
            "Mobile Backup to Filesystem Comparison Tool\n\n"
            "Compare mobile backup data to filesystem acquisitions\n"
            "for forensic analysis.\n\n"
            "Supports:\n"
            "- iOS backups (UFADE-style domain mappings with\n"
            "  container metadata resolution)\n"
            "- Android backups (.ab files with AOSP domain\n"
            "  token mappings)\n\n"
            "Backup type is auto-detected."
        )


def main():
    app = MainApplication()
    app.mainloop()


if __name__ == '__main__':
    main()
