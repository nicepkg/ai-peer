"""Unit tests for ai_peer.crypto — E2E encryption."""
import pytest

crypto = pytest.importorskip("cryptography", reason="cryptography not installed")

from ai_peer.crypto import encrypt, decrypt, derive_key, is_encrypted


class TestDeriveKey:
    def test_deterministic(self):
        k1 = derive_key("password", "room-123")
        k2 = derive_key("password", "room-123")
        assert k1 == k2

    def test_different_password(self):
        k1 = derive_key("pass1", "room-123")
        k2 = derive_key("pass2", "room-123")
        assert k1 != k2

    def test_different_room(self):
        k1 = derive_key("password", "room-a")
        k2 = derive_key("password", "room-b")
        assert k1 != k2


class TestEncryptDecrypt:
    def test_roundtrip(self):
        plain = "Hello, 你好世界! 🌍"
        cipher = encrypt(plain, "secret", "room-1")
        assert cipher != plain
        assert is_encrypted(cipher)
        result = decrypt(cipher, "secret", "room-1")
        assert result == plain

    def test_wrong_password(self):
        cipher = encrypt("secret message", "correct", "room-1")
        result = decrypt(cipher, "wrong", "room-1")
        assert result == "[encrypted — wrong password or corrupted]"

    def test_plaintext_passthrough(self):
        result = decrypt("just a normal message", "any", "room-1")
        assert result == "just a normal message"

    def test_chinese_password(self):
        plain = "机密信息"
        cipher = encrypt(plain, "今天天气好", "room-测试")
        result = decrypt(cipher, "今天天气好", "room-测试")
        assert result == plain

    def test_empty_message(self):
        cipher = encrypt("", "pw", "room")
        result = decrypt(cipher, "pw", "room")
        assert result == ""


class TestIsEncrypted:
    def test_encrypted(self):
        cipher = encrypt("test", "pw", "room")
        assert is_encrypted(cipher)

    def test_not_encrypted(self):
        assert not is_encrypted("hello world")
        assert not is_encrypted("")
        assert not is_encrypted("gAAA short")
