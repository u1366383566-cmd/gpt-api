"""ACCOUNT-IMPORT-PROXY-20261021 单测：导入载荷剥离注册专用代理字段。
运行：临时容器 --network none，挂仓库 /src:ro，sys.path 优先取改造后源码。
"""
import sys

sys.path.insert(0, "/src")

from services.account_service import AccountService  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond:
        FAILURES.append(name)


PROXY = "http://user:pass@asia.gw.proxylane.dev:10001"

# 1) 注册器旧版导出（proxy 随 dict 进来）必须被剥离
payload = AccountService._prepare_account_payload({
    "access_token": "tok-1",
    "email": "a@b.c",
    "proxy": PROXY,
    "proxy_used": PROXY,
    "quota": 25,
})
check("payload kept", payload is not None)
check("proxy stripped", "proxy" not in (payload or {}))
check("proxy_used stripped", "proxy_used" not in (payload or {}))
check("other fields kept", (payload or {}).get("quota") == 25 and (payload or {}).get("email") == "a@b.c")
check("access_token normalized", (payload or {}).get("access_token") == "tok-1")

# 2) 干净导入（新版导出，无 proxy）不受影响
payload2 = AccountService._prepare_account_payload({"accessToken": "tok-2", "email": "c@d.e"})
check("clean import ok", payload2 is not None and "proxy" not in (payload2 or {}))
check("accessToken normalized", (payload2 or {}).get("access_token") == "tok-2")

# 3) codex 导出格式归位不回归
payload3 = AccountService._prepare_account_payload({"access_token": "tok-3", "type": "codex", "proxy": PROXY})
check("codex source_type", (payload3 or {}).get("source_type") == "codex" and "type" not in (payload3 or {}))
check("codex proxy stripped", "proxy" not in (payload3 or {}))

# 4) 空/非法载荷仍返回 None
check("non-dict -> None", AccountService._prepare_account_payload("x") is None)
check("no-token -> None", AccountService._prepare_account_payload({"email": "x@y.z"}) is None)

print("ALL PASS" if not FAILURES else "FAILED: " + ", ".join(FAILURES), flush=True)
sys.exit(1 if FAILURES else 0)
