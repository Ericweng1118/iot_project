"""帳號密碼與角色（core/auth.py）。"""

from core.auth import has_role, hash_password, validate_new_password, verify_password


def test_hash_and_verify():
    h = hash_password("Secret123", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$")
    assert verify_password("Secret123", h)
    assert not verify_password("secret123", h)


def test_hash_is_salted():
    assert hash_password("same-Pass1", iterations=1000) != hash_password("same-Pass1", iterations=1000)


def test_verify_rejects_garbage():
    assert not verify_password("x", "")
    assert not verify_password("x", "md5$abc")
    assert not verify_password("x", "pbkdf2_sha256$notanumber$a$b")


def test_role_hierarchy():
    assert has_role("admin", "viewer")
    assert has_role("engineer", "operator")
    assert has_role("operator", "operator")
    assert not has_role("viewer", "operator")
    assert not has_role("hacker", "viewer")


def test_password_policy():
    assert validate_new_password("short1") is not None
    assert validate_new_password("12345678") is not None
    assert validate_new_password("abcdefgh") is not None
    assert validate_new_password("abcd1234") is None
