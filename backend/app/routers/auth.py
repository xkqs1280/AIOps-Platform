from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.services.auth_service import ROLES, create_access_token, decode_access_token, hash_password, require_password_strength, verify_password
from app.services.rate_limit import limit_login, record_login_failure
from app.services.audit_service import record_audit, get_client_ip

router = APIRouter(prefix="/auth", tags=["认证与用户"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class UserCreate(LoginRequest):
    role: str = "viewer"


class UserUpdate(BaseModel):
    role: str | None = None
    password: str | None = Field(None, min_length=12, max_length=256)
    is_active: bool | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=12, max_length=256)


def user_data(user: User) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role, "is_active": user.is_active, "created_at": user.created_at}


async def current_user(access_token: str | None = Cookie(None), authorization: str | None = Header(None), db: AsyncSession = Depends(get_db)) -> dict:
    token = access_token
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    identity = decode_access_token(token)
    # 从数据库加载真实角色与激活状态，避免降权/停用后旧 token 仍持有管理员权限
    account = (await db.execute(select(User).where(User.username == identity["sub"]))).scalar_one_or_none()
    if not account or not account.is_active:
        raise HTTPException(status_code=401, detail="用户已停用或不存在")
    return {"sub": account.username, "role": account.role, "is_active": account.is_active}


def admin_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    client = await limit_login(request)
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        record_login_failure(client, body.username)
        await record_audit(db, None, "auth", "login_failed", f"用户 {body.username} 登录失败", get_client_ip(request))
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(user.username, user.role)
    response.set_cookie("access_token", token, httponly=True, samesite="lax", secure=settings.COOKIE_SECURE, max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    await record_audit(db, {"sub": user.username, "role": user.role}, "auth", "login", f"用户 {user.username} 登录成功", get_client_ip(request))
    # 返回体同时携带 access_token：桌面端继续走 cookie，手机端（跨域）用 Bearer header
    data = user_data(user)
    data["access_token"] = token
    data["token_type"] = "bearer"
    return data


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    username = "-"
    try:
        token = request.cookies.get("access_token")
        if token:
            identity = decode_access_token(token)
            username = identity.get("sub", "-")
    except Exception:
        pass
    response.delete_cookie("access_token")
    await record_audit(db, {"sub": username, "role": ""}, "auth", "logout", f"用户 {username} 退出登录", get_client_ip(request))
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return {"username": user["sub"], "role": user["role"]}


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, actor: dict = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """当前登录用户修改自己的密码（需校验旧密码）。"""
    user = (await db.execute(select(User).where(User.username == actor["sub"]))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")
    require_password_strength(body.new_password)
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    await record_audit(db, actor, "auth", "change_password", f"用户 {actor['sub']} 修改密码", "")
    return {"ok": True, "message": "密码修改成功"}


@router.get("/users")
async def list_users(_: dict = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    return [user_data(user) for user in (await db.execute(select(User).order_by(User.id))).scalars().all()]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, actor: dict = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    if body.role not in ROLES:
        raise HTTPException(status_code=422, detail="无效角色")
    require_password_strength(body.password)
    if (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(user); await db.commit(); await db.refresh(user)
    await record_audit(db, actor, "user", "create", f"创建用户 {body.username}（角色 {body.role}）", "")
    return user_data(user)


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdate, actor: dict = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user: raise HTTPException(status_code=404, detail="用户不存在")
    if body.role is not None:
        if body.role not in ROLES: raise HTTPException(status_code=422, detail="无效角色")
        user.role = body.role
    if body.password is not None:
        require_password_strength(body.password); user.password_hash = hash_password(body.password)
    if body.is_active is not None:
        if user.username == actor["sub"] and not body.is_active: raise HTTPException(status_code=400, detail="不能停用自己")
        user.is_active = body.is_active
    await db.commit(); await db.refresh(user)
    await record_audit(db, actor, "user", "update", f"更新用户 {user.username}（角色/密码/状态）", "")
    return user_data(user)
