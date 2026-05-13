# agent/auth.py — Utilidades de autenticación para el panel admin

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

ADMIN_USERNAME = "hgarza"
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = 24


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET no está configurado en las variables de entorno")
    return secret


def verify_password(plain: str) -> bool:
    """
    Verifica contraseña en texto plano contra el hash en ADMIN_PASSWORD_HASH.
    Compatible con hashes generados por bcrypt 4.x directamente.
    """
    stored_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
    if not stored_hash:
        return False
    return bcrypt.checkpw(plain.encode("utf-8"), stored_hash.encode("utf-8"))


def create_token() -> str:
    """Genera un JWT firmado con expiración de 24h."""
    expire = datetime.now(timezone.utc) + timedelta(hours=_JWT_EXPIRE_HOURS)
    payload = {"sub": ADMIN_USERNAME, "exp": expire}
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


def verify_token(token: str) -> bool:
    """Valida un JWT. Retorna True si es válido y no expiró."""
    try:
        claims = jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
        return claims.get("sub") == ADMIN_USERNAME
    except JWTError:
        return False
