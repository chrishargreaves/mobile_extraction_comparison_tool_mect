"""Tests for android_backup_parser module."""

import pytest

from android_backup_parser import (
    parse_tar_path,
    AndroidBackupFile,
    AndroidBackup,
    UNMAPPABLE_TOKENS,
    KNOWN_TOKENS,
    TOKEN_PATH_MAPPINGS,
)


class TestParseTarPath:
    """Tests for the parse_tar_path() module-level function."""

    def test_app_with_db_token(self):
        domain, token, rel = parse_tar_path("apps/com.whatsapp/db/msgstore.db")
        assert domain == "com.whatsapp"
        assert token == "db"
        assert rel == "db/msgstore.db"

    def test_app_with_apk_token(self):
        domain, token, rel = parse_tar_path("apps/com.example/a/base.apk")
        assert domain == "com.example"
        assert token == "a"
        assert rel == "a/base.apk"

    def test_app_with_manifest_token(self):
        domain, token, rel = parse_tar_path("apps/com.example/_manifest")
        assert domain == "com.example"
        assert token == "_manifest"
        assert rel == "_manifest"

    def test_app_with_sp_token(self):
        domain, token, rel = parse_tar_path("apps/com.example/sp/prefs.xml")
        assert domain == "com.example"
        assert token == "sp"
        assert rel == "sp/prefs.xml"

    def test_app_with_r_token(self):
        domain, token, rel = parse_tar_path("apps/com.example/r/some/file.txt")
        assert domain == "com.example"
        assert token == "r"
        assert rel == "r/some/file.txt"

    def test_app_with_f_token(self):
        domain, token, rel = parse_tar_path("apps/com.example/f/myfile.txt")
        assert domain == "com.example"
        assert token == "f"
        assert rel == "f/myfile.txt"

    def test_app_with_ef_token(self):
        domain, token, rel = parse_tar_path("apps/com.example/ef/data.txt")
        assert domain == "com.example"
        assert token == "ef"
        assert rel == "ef/data.txt"

    def test_app_with_k_token(self):
        domain, token, rel = parse_tar_path("apps/com.example/k")
        assert domain == "com.example"
        assert token == "k"
        assert rel == "k"

    def test_app_with_unknown_token(self):
        """Unknown tokens are still returned as the token."""
        domain, token, rel = parse_tar_path("apps/com.example/zz/file.txt")
        assert domain == "com.example"
        assert token == "zz"
        assert rel == "zz/file.txt"

    def test_app_package_only(self):
        domain, token, rel = parse_tar_path("apps/com.example")
        assert domain == "com.example"
        assert token == ""
        assert rel == ""

    def test_shared_storage(self):
        domain, token, rel = parse_tar_path("shared/0/DCIM/photo.jpg")
        assert domain == "shared/0"
        assert token == ""
        assert rel == "DCIM/photo.jpg"

    def test_shared_storage_user_1(self):
        domain, token, rel = parse_tar_path("shared/1/Music/song.mp3")
        assert domain == "shared/1"
        assert token == ""
        assert rel == "Music/song.mp3"

    def test_shared_root(self):
        domain, token, rel = parse_tar_path("shared/0")
        assert domain == "shared/0"
        assert token == ""
        assert rel == ""

    def test_leading_dot_slash(self):
        """Leading ./ should be stripped."""
        domain, token, rel = parse_tar_path("./apps/com.example/db/data.db")
        assert domain == "com.example"
        assert token == "db"
        assert rel == "db/data.db"

    def test_leading_slash(self):
        """Leading / should be stripped."""
        domain, token, rel = parse_tar_path("/apps/com.example/db/data.db")
        assert domain == "com.example"
        assert token == "db"
        assert rel == "db/data.db"

    def test_unknown_top_level(self):
        """Non-apps/shared paths return first part as domain."""
        domain, token, rel = parse_tar_path("other/some/path")
        assert domain == "other"
        assert token == ""
        assert rel == "some/path"

    def test_nested_path_in_db(self):
        domain, token, rel = parse_tar_path("apps/com.pkg/db/subdir/file.db")
        assert domain == "com.pkg"
        assert token == "db"
        assert rel == "db/subdir/file.db"

    def test_device_encrypted_tokens(self):
        """Device-encrypted tokens (d_*) should be recognized."""
        domain, token, rel = parse_tar_path("apps/com.pkg/d_db/data.db")
        assert domain == "com.pkg"
        assert token == "d_db"
        assert rel == "d_db/data.db"


class TestAndroidBackupFileIsDirectory:
    """Tests for AndroidBackupFile.is_directory property — covers the mode bit fix."""

    def test_standard_directory_mode(self):
        f = AndroidBackupFile("id", "pkg", "dir", 0, mode=0o040755, token="r")
        assert f.is_directory is True

    def test_standard_file_mode(self):
        f = AndroidBackupFile("id", "pkg", "file.txt", 1024, mode=0o100644, token="r")
        assert f.is_directory is False

    def test_mode_without_type_bits_is_not_directory(self):
        """Mode 0o771 (no type bits) should NOT be a directory — this was the bug."""
        f = AndroidBackupFile("id", "pkg", "dir", 0, mode=0o771, token="r")
        assert f.is_directory is False

    def test_mode_with_directory_bit_added(self):
        """Mode 0o040771 should be a directory — this is what the fix produces."""
        f = AndroidBackupFile("id", "pkg", "dir", 0, mode=0o040771, token="r")
        assert f.is_directory is True

    def test_zero_mode(self):
        f = AndroidBackupFile("id", "pkg", "file", 0, mode=0, token="r")
        assert f.is_directory is False

    def test_symlink_mode(self):
        f = AndroidBackupFile("id", "pkg", "link", 0, mode=0o120777, token="r")
        assert f.is_directory is False

    def test_executable_file(self):
        f = AndroidBackupFile("id", "pkg", "script.sh", 100, mode=0o100755, token="r")
        assert f.is_directory is False


class TestAndroidBackupFileFullDomainPath:
    """Tests for AndroidBackupFile.full_domain_path property."""

    def test_with_relative_path(self):
        f = AndroidBackupFile("id", "com.whatsapp", "db/msgstore.db", 1024, mode=0o100644, token="db")
        assert f.full_domain_path == "com.whatsapp/db/msgstore.db"

    def test_without_relative_path(self):
        f = AndroidBackupFile("id", "com.whatsapp", "", 0, mode=0o040755, token="")
        assert f.full_domain_path == "com.whatsapp"

    def test_shared_domain(self):
        f = AndroidBackupFile("id", "shared/0", "DCIM/photo.jpg", 2048, mode=0o100644, token="")
        assert f.full_domain_path == "shared/0/DCIM/photo.jpg"


class TestAndroidBackupGetFilesByDomain:
    """Tests for AndroidBackup.get_files_by_domain()."""

    def test_multiple_domains(self, make_android_file):
        files = [
            make_android_file(domain="com.whatsapp", relative_path="db/msgstore.db"),
            make_android_file(domain="com.whatsapp", relative_path="sp/prefs.xml"),
            make_android_file(domain="com.example", relative_path="r/data.txt"),
        ]
        backup = AndroidBackup(path="/fake.ab", files=files, manifest_db_row_count=3)
        by_domain = backup.get_files_by_domain()

        assert len(by_domain) == 2
        assert len(by_domain["com.whatsapp"]) == 2
        assert len(by_domain["com.example"]) == 1

    def test_empty_files(self):
        backup = AndroidBackup(path="/fake.ab", files=[], manifest_db_row_count=0)
        assert backup.get_files_by_domain() == {}


class TestUnmappableTokens:
    """Tests for token constants."""

    def test_manifest_is_unmappable(self):
        assert "_manifest" in UNMAPPABLE_TOKENS

    def test_k_is_unmappable(self):
        assert "k" in UNMAPPABLE_TOKENS

    def test_db_is_not_unmappable(self):
        assert "db" not in UNMAPPABLE_TOKENS

    def test_known_tokens_includes_all(self):
        for token in TOKEN_PATH_MAPPINGS:
            assert token in KNOWN_TOKENS
        for token in UNMAPPABLE_TOKENS:
            assert token in KNOWN_TOKENS
