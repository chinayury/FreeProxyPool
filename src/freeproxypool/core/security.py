# -*- coding: utf-8 -*-
"""密码哈希、令牌生成、常量时间比较。"""
import hashlib
import hmac
import secrets

#: PBKDF2 迭代次数。12 万次是 OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议下限。
PBKDF2_ITERATIONS = 120_000
PBKDF2_ALGORITHM = "sha256"
PBKDF2_PREFIX = "pbkdf2_sha256"

#: 管理员口令最小长度。短口令在管理后台被爆破的成本极低，直接拒绝。
MIN_PASSWORD_LENGTH = 10


def constant_time_compare(left, right) -> bool:
    """恒定时长比较，抵御时序侧信道。

    ``None`` 统一按字符串 ``"None"`` 比较——调用方通常已经判断过
    "是否提供"，这里保证任何输入都不会抛异常。
    """
    if left is None:
        left = ""
    if right is None:
        right = ""
    return hmac.compare_digest(
        str(left).encode("utf-8"),
        str(right).encode("utf-8"),
    )


def hash_password(password: str, salt: bytes = None) -> str:
    """生成 PBKDF2 哈希串：``pbkdf2_sha256$iter$salt_b64$hash_b64``。"""
    if salt is None:
        salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM, str(password).encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "%s$%d$%s$%s" % (
        PBKDF2_PREFIX,
        PBKDF2_ITERATIONS,
        _b64(salt),
        _b64(derived),
    )


def verify_password_hash(password: str, stored: str) -> bool:
    """校验 PBKDF2 哈希，任何格式/解析异常都返回 ``False``（fail-closed）。"""
    try:
        algo, iterations, salt_b64, hash_b64 = str(stored).split("$", 3)
        if algo != PBKDF2_PREFIX:
            return False
        derived = hashlib.pbkdf2_hmac(
            PBKDF2_ALGORITHM,
            str(password).encode("utf-8"),
            _unb64(salt_b64),
            int(iterations),
        )
        return hmac.compare_digest(derived, _unb64(hash_b64))
    except (ValueError, TypeError, AttributeError):
        return False


def is_hashed(value: str) -> bool:
    """判断存储值是否为 PBKDF2 哈希格式。"""
    return isinstance(value, str) and value.startswith(PBKDF2_PREFIX + "$")


def generate_token(nbytes: int = 32) -> str:
    """生成高熵随机令牌（``secrets.token_urlsafe``，默认 256 bit 熵）。"""
    return secrets.token_urlsafe(nbytes)


def derive_csrf(session_token: str, secret: str) -> str:
    """由会话令牌 + 服务端密钥确定性派生 CSRF 令牌。

    采用 ``HMAC-SHA256(key=服务端密钥, msg=会话令牌)``。
    不落库 —— 服务端随时可由会话令牌重算出同一值，
    天然支持多网关副本，避免"写入失败被吞导致校验随机失败"的经典问题。
    """
    if not session_token or not secret:
        return ""
    return hmac.new(
        str(secret).encode("utf-8"),
        str(session_token).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def password_strength_error(password: str) -> str:
    """返回口令强度问题描述；无问题返回空串。

    仅做"最低限度"的硬性拦截（长度 + 字符类型），不做黑名单字典比对 ——
    后者会让用户无法预测为什么被拒，对自助式运维台得不偿失。
    """
    value = str(password or "")
    if len(value) < MIN_PASSWORD_LENGTH:
        return "管理密码长度至少 %d 位" % MIN_PASSWORD_LENGTH
    kinds = 0
    kinds += 1 if any(c.islower() for c in value) else 0
    kinds += 1 if any(c.isupper() for c in value) else 0
    kinds += 1 if any(c.isdigit() for c in value) else 0
    kinds += 1 if any(not c.isalnum() for c in value) else 0
    if kinds < 3:
        return "管理密码需至少包含大写、小写、数字、特殊字符中的 3 类"
    return ""


def _b64(raw: bytes) -> str:
    from base64 import b64encode
    return b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    from base64 import b64decode
    return b64decode(str(text).encode("ascii"))


__all__ = [
    "PBKDF2_ITERATIONS", "PBKDF2_PREFIX", "MIN_PASSWORD_LENGTH",
    "constant_time_compare", "hash_password", "verify_password_hash",
    "is_hashed", "generate_token", "derive_csrf", "password_strength_error",
]