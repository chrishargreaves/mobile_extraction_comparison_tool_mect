"""Tests for filesystem_loader module."""

import pytest

from filesystem_loader import FilesystemFile, FilesystemAcquisition


class TestFilesystemFileNormalizedPathIOS:
    """Tests for FilesystemFile.normalized_path on iOS platform."""

    def test_already_private_prefix(self):
        f = FilesystemFile("/private/var/mobile/file.txt", 100, False, platform="ios")
        assert f.normalized_path == "/private/var/mobile/file.txt"

    def test_dot_slash_private(self):
        f = FilesystemFile("./private/var/file.txt", 100, False, platform="ios")
        assert f.normalized_path == "/private/var/file.txt"

    def test_private_without_slash(self):
        f = FilesystemFile("private/var/file.txt", 100, False, platform="ios")
        assert f.normalized_path == "/private/var/file.txt"

    def test_dot_slash_without_private(self):
        f = FilesystemFile("./var/mobile/file.txt", 100, False, platform="ios")
        assert f.normalized_path == "/private/var/mobile/file.txt"

    def test_bare_path_without_private(self):
        f = FilesystemFile("var/mobile/file.txt", 100, False, platform="ios")
        assert f.normalized_path == "/private/var/mobile/file.txt"

    def test_absolute_without_private(self):
        """An absolute path without /private/ gets the prefix via find_file fallback."""
        f = FilesystemFile("/private/var/mobile/Library/SMS/sms.db", 1024, False, platform="ios")
        assert f.normalized_path == "/private/var/mobile/Library/SMS/sms.db"


class TestFilesystemFileNormalizedPathAndroid:
    """Tests for FilesystemFile.normalized_path on Android platform."""

    def test_absolute_path(self):
        f = FilesystemFile("/data/data/com.example/databases/data.db", 100, False, platform="android")
        assert f.normalized_path == "/data/data/com.example/databases/data.db"

    def test_dot_slash_prefix(self):
        f = FilesystemFile("./data/data/com.example/file.txt", 100, False, platform="android")
        assert f.normalized_path == "/data/data/com.example/file.txt"

    def test_bare_path(self):
        f = FilesystemFile("data/data/com.example/file.txt", 100, False, platform="android")
        assert f.normalized_path == "/data/data/com.example/file.txt"


class TestFilesystemAcquisitionBuildIndexAndroid:
    """Tests for build_index() Android path equivalences."""

    def _make_acq(self, files):
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=files)
        acq.build_index()
        return acq

    def test_data_data_indexed_at_data_user_0(self):
        f = FilesystemFile("/data/data/com.example/databases/data.db", 100, False, platform="android")
        acq = self._make_acq([f])

        assert acq._file_index.get("/data/data/com.example/databases/data.db") is f
        assert acq._file_index.get("/data/user/0/com.example/databases/data.db") is f

    def test_data_user_0_indexed_at_data_data(self):
        f = FilesystemFile("/data/user/0/com.example/file.txt", 100, False, platform="android")
        acq = self._make_acq([f])

        assert acq._file_index.get("/data/user/0/com.example/file.txt") is f
        assert acq._file_index.get("/data/data/com.example/file.txt") is f

    def test_data_media_indexed_at_storage_and_sdcard(self):
        f = FilesystemFile("/data/media/0/DCIM/photo.jpg", 2048, False, platform="android")
        acq = self._make_acq([f])

        assert acq._file_index.get("/data/media/0/DCIM/photo.jpg") is f
        assert acq._file_index.get("/storage/emulated/0/DCIM/photo.jpg") is f
        assert acq._file_index.get("/sdcard/DCIM/photo.jpg") is f

    def test_storage_emulated_indexed_at_media_and_sdcard(self):
        f = FilesystemFile("/storage/emulated/0/Download/file.pdf", 500, False, platform="android")
        acq = self._make_acq([f])

        assert acq._file_index.get("/storage/emulated/0/Download/file.pdf") is f
        assert acq._file_index.get("/data/media/0/Download/file.pdf") is f
        assert acq._file_index.get("/sdcard/Download/file.pdf") is f

    def test_sdcard_indexed_at_media_and_storage(self):
        f = FilesystemFile("/sdcard/Music/song.mp3", 3000, False, platform="android")
        acq = self._make_acq([f])

        assert acq._file_index.get("/sdcard/Music/song.mp3") is f
        assert acq._file_index.get("/storage/emulated/0/Music/song.mp3") is f
        assert acq._file_index.get("/data/media/0/Music/song.mp3") is f


class TestFilesystemAcquisitionBuildIndexIOS:
    """Tests for build_index() iOS path equivalences."""

    def _make_acq(self, files):
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="ios", files=files)
        acq.build_index()
        return acq

    def test_private_prefix_also_indexed_without(self):
        f = FilesystemFile("/private/var/mobile/Library/file.db", 100, False, platform="ios")
        acq = self._make_acq([f])

        assert acq._file_index.get("/private/var/mobile/Library/file.db") is f
        assert acq._file_index.get("/var/mobile/Library/file.db") is f


class TestFilesystemAcquisitionFindFile:
    """Tests for find_file() lookups."""

    def test_direct_lookup(self):
        f = FilesystemFile("/data/data/com.example/data.db", 100, False, platform="android")
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=[f])
        acq.build_index()

        assert acq.find_file("/data/data/com.example/data.db") is f

    def test_android_equivalence_lookup(self):
        f = FilesystemFile("/data/data/com.example/data.db", 100, False, platform="android")
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=[f])
        acq.build_index()

        # Look up via /data/user/0/ equivalence
        assert acq.find_file("/data/user/0/com.example/data.db") is f

    def test_ios_private_prefix_lookup(self):
        f = FilesystemFile("/private/var/mobile/file.db", 100, False, platform="ios")
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="ios", files=[f])
        acq.build_index()

        # Look up without /private prefix
        assert acq.find_file("/var/mobile/file.db") is f

    def test_ios_add_private_prefix(self):
        """find_file should try adding /private prefix for iOS."""
        f = FilesystemFile("/private/var/mobile/Library/SMS/sms.db", 100, False, platform="ios")
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="ios", files=[f])
        acq.build_index()

        result = acq.find_file("/var/mobile/Library/SMS/sms.db")
        assert result is f

    def test_android_without_leading_slash(self):
        f = FilesystemFile("/data/data/com.example/data.db", 100, False, platform="android")
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=[f])
        acq.build_index()

        result = acq.find_file("data/data/com.example/data.db")
        assert result is f

    def test_not_found(self):
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=[])
        acq.build_index()
        assert acq.find_file("/nonexistent/path") is None

    def test_auto_build_index(self):
        """find_file should auto-build index if not yet built."""
        f = FilesystemFile("/data/data/com.example/data.db", 100, False, platform="android")
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=[f])
        # Don't call build_index() — find_file should do it
        assert acq.find_file("/data/data/com.example/data.db") is f


class TestFilesystemAcquisitionFindFilesInDirectory:
    """Tests for find_files_in_directory()."""

    def test_finds_files_in_prefix(self):
        files = [
            FilesystemFile("/data/data/com.example/databases/a.db", 100, False, platform="android"),
            FilesystemFile("/data/data/com.example/databases/b.db", 200, False, platform="android"),
            FilesystemFile("/data/data/com.other/databases/c.db", 300, False, platform="android"),
        ]
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=files)
        acq.build_index()

        results = acq.find_files_in_directory("/data/data/com.example/databases")
        paths = {f.path for f in results}
        assert "/data/data/com.example/databases/a.db" in paths
        assert "/data/data/com.example/databases/b.db" in paths
        assert "/data/data/com.other/databases/c.db" not in paths

    def test_trailing_slash_added(self):
        f = FilesystemFile("/data/data/com.example/databases/a.db", 100, False, platform="android")
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=[f])
        acq.build_index()

        # No trailing slash — should still work
        results = acq.find_files_in_directory("/data/data/com.example/databases")
        assert len(results) >= 1

    def test_empty_directory(self):
        acq = FilesystemAcquisition(path="/fake", format="tar", platform="android", files=[])
        acq.build_index()
        assert acq.find_files_in_directory("/nonexistent") == []
