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


def test_shared_login_vector_with_pme():
    """
    PME 共用 app_users 登入，自己實作同格式的 PBKDF2 驗證（PME 的 core/security.py）。
    兩邊放同一組測試向量：SCADA 換雜湊格式或演算法時，兩邊的這個測試會一起失敗，不會悄悄讓 PME 無法登入。
    """
    vector = "pbkdf2_sha256$260000$Ll/ReHS3Bie0gZmOacMGaw==$di5At4qKMcS43IiZaNZijGqfjQEu8c4VDVbmrlZH6PM="
    assert verify_password("pme-contract-test", vector)
    assert not verify_password("pme-contract-tesT", vector)
