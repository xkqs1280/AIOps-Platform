from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
ROLES = {"admin", "operator", "viewer"}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(username: str, role: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "role": role, "exp": expires}, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效") from error
    if not payload.get("sub") or payload.get("role") not in ROLES:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效登录令牌")
    return payload


def require_password_strength(password: str) -> None:
    if len(password) < 12 or password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        raise HTTPException(status_code=422, detail="密码至少 12 位，并包含大小写字母和数字")
