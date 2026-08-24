"""Small in-process rate limiter for unauthenticated endpoints (ingest / login)."""
from collections import defaultdict, deque
from time import monotonic

from fastapi import HTTPException, Request

WINDOW_SECONDS = 60
MAX_REQUESTS = 120
_requests: dict[str, deque[float]] = defaultdict(deque)


async def limit_ingest(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    now = monotonic()
    bucket = _requests[client]
    while bucket and bucket[0] <= now - WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    bucket.append(now)


# ---- 登录限流 / 失败锁定 ----
IP_ATTEMPT_WINDOW = 60      # IP 每分钟最多登录尝试
IP_ATTEMPT_MAX = 30
FAIL_WINDOW = 600           # 失败锁定窗口（10 分钟）
FAIL_MAX = 5                # 窗口内失败 5 次锁定

_attempts: dict[str, deque[float]] = defaultdict(deque)          # ip -> 尝试时间
_failures: dict[str, deque[float]] = defaultdict(deque)          # ip -> 失败时间
_username_fail: dict[str, deque[float]] = defaultdict(deque)     # username -> 失败时间


def _prune(dq: deque, window: float) -> None:
    cutoff = monotonic() - window
    while dq and dq[0] <= cutoff:
        dq.popleft()


async def limit_login(request: Request) -> str:
    """登录前限流检查：返回客户端 IP。超限抛 429。"""
    client = request.client.host if request.client else "unknown"
    now = monotonic()
    # IP 总尝试限流（每分钟）
    bucket = _attempts[client]
    _prune(bucket, IP_ATTEMPT_WINDOW)
    if len(bucket) >= IP_ATTEMPT_MAX:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    bucket.append(now)
    # IP 失败锁定
    fails = _failures[client]
    _prune(fails, FAIL_WINDOW)
    if len(fails) >= FAIL_MAX:
        raise HTTPException(status_code=429, detail="失败次数过多，已临时锁定，请 10 分钟后再试")
    return client


def record_login_failure(client: str, username: str) -> None:
    """记录一次登录失败（IP + 用户名），用于锁定。"""
    now = monotonic()
    _failures[client].append(now)
    _prune(_failures[client], FAIL_WINDOW)
    _username_fail[username.lower()].append(now)
    _prune(_username_fail[username.lower()], FAIL_WINDOW)
