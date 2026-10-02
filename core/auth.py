"""
core/auth.py
============
網頁後台的帳號與角色權限。

角色（由低到高，高階角色擁有低階角色的全部權限）：
    viewer    檢視：總覽、趨勢、報表、警報清單、系統狀態
    operator  操作員：+ 確認（ACK）警報
    engineer  工程師：+ 點位 / 感測器階層 / 警報規則等所有設定
    admin     管理員：+ 使用者管理、稽核紀錄

帳號來源：
    1. app_users 資料表（sql/012 建立），密碼以 PBKDF2-SHA256 雜湊儲存
    2. .env 的 ADMIN_USER / ADMIN_PASSWORD —— 永遠視為 admin，當作救援帳號，
       避免 app_users 還沒建立或管理員把自己鎖在外面時完全進不去。
       這也是 v2 以前唯一的登入方式，保留它可以讓既有部署升級後照常登入。

密碼雜湊只用標準函式庫（hashlib.pbkdf2_hmac），不額外引入 bcrypt 等相依套件。
"""

import base64
import hashlib
import hmac
import logging
import secrets

from core.config import env_str

logger = logging.getLogger(__name__)

ROLES = ("viewer", "operator", "engineer", "admin")
ROLE_LABELS = {
    "viewer": "檢視者",
    "operator": "操作員",
    "engineer": "工程師",
    "admin": "管理員",
}

_PBKDF2_ITERATIONS = 260_000
_HASH_PREFIX = "pbkdf2_sha256"


def hash_password(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """回傳 `pbkdf2_sha256$<iterations>$<salt>$<hash>` 格式的字串。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([
        _HASH_PREFIX,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        prefix, iterations, salt_b64, hash_b64 = stored_hash.split("$")
        if prefix != _HASH_PREFIX:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def has_role(user_role: str, required: str) -> bool:
    """user_role 是否達到 required 的層級。未知角色一律視為無權限。"""
    if user_role not in ROLES or required not in ROLES:
        return False
    return ROLES.index(user_role) >= ROLES.index(required)


def validate_new_password(password: str) -> str | None:
    """回傳錯誤訊息；合格時回傳 None。"""
    if len(password) < 8:
        return "密碼至少需要 8 個字元"
    if password.isdigit() or password.isalpha():
        return "密碼需同時包含英文字母與數字（或符號）"
    return None


def _env_admin():
    return env_str("ADMIN_USER", "USER"), env_str("ADMIN_PASSWORD", "PASSWORD")


def is_env_admin(username: str) -> bool:
    return username == _env_admin()[0]


def authenticate(username: str, password: str):
    """
    驗證帳密。成功回傳 dict(username, display_name, role, source)，失敗回傳 None。
    資料庫查詢失敗（例如 sql/012 還沒跑）時只會退回 .env 救援帳號，不會整個登入壞掉。
    """
    from data_layer.db_connector import DatabaseConnector

    username = (username or "").strip()
    if not username or not password:
        return None

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT password_hash, display_name, role, enabled "
                    "FROM app_users WHERE username = %s;",
                    (username,),
                )
                row = cur.fetchone()
                if row:
                    password_hash, display_name, role, enabled = row
                    if enabled and verify_password(password, password_hash):
                        cur.execute(
                            "UPDATE app_users SET last_login = now() WHERE username = %s;",
                            (username,),
                        )
                        return {
                            "username": username,
                            "display_name": display_name or username,
                            "role": role,
                            "source": "db",
                        }
                    return None
    except Exception as e:
        logger.warning(f"⚠️ 讀取 app_users 失敗，僅能使用 .env 救援帳號登入: {e}")

    env_user, env_password = _env_admin()
    if hmac.compare_digest(username, env_user) and hmac.compare_digest(password, env_password):
        return {
            "username": env_user,
            "display_name": env_user,
            "role": "admin",
            "source": "env",
        }
    return None
