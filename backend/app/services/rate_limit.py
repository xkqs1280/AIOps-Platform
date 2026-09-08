"""Small in-process rate limiter for unauthenticated endpoints (ingest / login).

设计约束（P1-5 修复）：
- 登录防爆破区分 IP 维度与账号维度：
  * IP 维度：单 IP 总尝试 / 失败次数超限即锁该 IP（攻击者须换 IP 才能继续）；
  * 账号维度：只有失败总数 >= ACCOUNT_FAIL_MAX 且来自 >= ACCOUNT_MIN_SOURCES 个
    不同来源 IP（即分布式爆破特征）才锁定账号本身 —— 杜绝“任意单 IP 刷几次
    错误密码即可无成本锁死 admin”的 DoS；
- 所有状态桶 TTL 到期即删；总桶数设上限 MAX_BUCKETS，超限逐出最旧 key，
  避免攻击者用随机 username / 伪造 IP 使进程内 dict 无限膨胀。
"""
from collections import deque
from time import monotonic

from fastapi import HTTPException, Request

WINDOW_SECONDS = 60
MAX_REQUESTS = 120
_requests: dict[str, deque[float]] = {}


# ---- 登录限流 / 失败锁定 ----
IP_ATTEMPT_WINDOW = 60      # IP 每分钟最多登录尝试
IP_ATTEMPT_MAX = 30
FAIL_WINDOW = 600           # IP 失败锁定窗口（10 分钟）
FAIL_MAX = 5                # 单 IP 窗口内失败 5 次锁定该 IP
ACCOUNT_WINDOW = 900        # 账号级锁定窗口（15 分钟）
ACCOUNT_FAIL_MAX = 10       # 账号失败总数阈值（跨来源）
ACCOUNT_MIN_SOURCES = 3     # 触发账号锁所需的最少不同来源 IP（分布式特征）
MAX_BUCKETS = 20_000        # 每个存储 dict 的 key 上限（防无限膨胀）

_attempts: dict[str, deque[float]] = {}                    # ip -> 尝试时间
_failures: dict[str, deque[float]] = {}                    # ip -> 失败时间
_username_fail: dict[str, deque[tuple[float, str]]] = {}   # username -> [(失败时间, 来源 ip)]


def _prune(dq: deque, window: float, *, pair: bool = False) -> None:
    """就地去掉窗口外的过期元素。pair=True 时元素为 (ts, ip)。"""
    cutoff = monotonic() - window
    if pair:
        while dq and dq[0][0] <= cutoff:
            dq.popleft()
    else:
        while dq and dq[0] <= cutoff:
            dq.popleft()


def _bucket(storage: dict, key: str, window: float, *, pair: bool = False) -> deque:
    """取 key 的桶（prune 过期后）。无桶或清空后返回新空桶并写回。"""
    dq = storage.get(key)
    if dq is None:
        dq = deque()
        storage[key] = dq
    _prune(dq, window, pair=pair)
    if not dq:
        dq.clear()
    return dq


def _maybe_sweep(storage: dict) -> None:
    """桶数超 MAX_BUCKETS 时清理空桶并逐出最旧 key，保证内存有界。"""
    if len(storage) <= MAX_BUCKETS:
        return
    # 清空空桶（正常情况下 prune 后已清，此处兜底）
    for k in [k for k, v in storage.items() if not v]:
        del storage[k]
    # dict 保持插入序：逐出最旧 key
    while len(storage) > MAX_BUCKETS:
        storage.pop(next(iter(storage)), None)


def _get_client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def limit_ingest(request: Request) -> None:
    client = _get_client(request)
    now = monotonic()
    dq = _bucket(_requests, client, WINDOW_SECONDS)
    if len(dq) >= MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    dq.append(now)
    _maybe_sweep(_requests)


def check_username_locked(username: str, client: str) -> None:
    """登录前检查：账号是否处于锁定。

    仅在“窗口内失败总数 >= ACCOUNT_FAIL_MAX 且来源 IP >= ACCOUNT_MIN_SOURCES”
    时锁账号（分布式爆破特征）。单来源大量失败由 IP 维度锁定负责，不会让
    单一攻击 IP 无成本把合法账号锁死（P1-5 防 DoS）。
    """
    if not username:
        return
    key = username.lower()
    dq = _username_fail.get(key)
    if not dq:
        return
    _prune(dq, ACCOUNT_WINDOW, pair=True)
    if not dq:
        _username_fail.pop(key, None)
        return
    if len(dq) >= ACCOUNT_FAIL_MAX and len({ip for _, ip in dq}) >= ACCOUNT_MIN_SOURCES:
        raise HTTPException(
            status_code=429,
            detail="检测到来自多个来源的异常登录尝试，该账号已临时锁定，请 15 分钟后再试",
        )


async def limit_login(request: Request) -> str:
    """登录前限流检查：返回客户端 IP。超限抛 429。"""
    client = _get_client(request)
    now = monotonic()
    # IP 总尝试限流（每分钟）
    dq = _bucket(_attempts, client, IP_ATTEMPT_WINDOW)
    if len(dq) >= IP_ATTEMPT_MAX:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    dq.append(now)
    # IP 失败锁定
    fails = _bucket(_failures, client, FAIL_WINDOW)
    if len(fails) >= FAIL_MAX:
        raise HTTPException(status_code=429, detail="失败次数过多，已临时锁定，请 10 分钟后再试")
    _maybe_sweep(_attempts)
    _maybe_sweep(_failures)
    return client


def record_login_failure(client: str, username: str) -> None:
    """记录一次登录失败（IP + 账号 + 来源），用于后续锁定判断。"""
    now = monotonic()
    fails = _bucket(_failures, client, FAIL_WINDOW)
    fails.append(now)
    if username:
        key = username.lower()
        ud = _bucket(_username_fail, key, ACCOUNT_WINDOW, pair=True)
        ud.append((now, client))
        _maybe_sweep(_username_fail)
    _maybe_sweep(_failures)


def reset_state() -> None:
    """清空全部限流状态（测试用）。"""
    _requests.clear()
    _attempts.clear()
    _failures.clear()
    _username_fail.clear()
