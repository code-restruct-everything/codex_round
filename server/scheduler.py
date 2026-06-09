import httpx
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from db import list_accounts, update_account, delete_account, get_account
from core import ensure_fresh_token, InvalidGrantError, get_auth_path

logger = logging.getLogger("vault.scheduler")

async def remove_account(account_id: str, reason: str):
    logger.warning(f"⚠️ 账号 {account_id} 已移除（原因：{reason}）")
    auth_path = get_auth_path(account_id)
    if auth_path.exists():
        auth_path.unlink()
    delete_account(account_id)
    # TODO: Send alert via email/slack

async def heartbeat_account(account_id: str):
    logger.info(f"Running heartbeat for account {account_id}")
    try:
        auth = await ensure_fresh_token(account_id)
    except InvalidGrantError:
        await remove_account(account_id, reason="refresh_token invalid")
        return
    except Exception as e:
        logger.error(f"Error refreshing token for {account_id}: {e}")
        return

    # 探活：用 chatgpt.com/backend-api/me，该端点接受 OAuth access_token
    # 注意：api.openai.com 要求的是 sk-xxx API Key，不接受 OAuth token，不能用于探活
    try:
        async with httpx.AsyncClient() as client:
            access_t = auth.get("access_token") or auth.get("accessToken") or (auth.get("tokens") or {}).get("access_token")
            resp = await client.get(
                "https://chatgpt.com/backend-api/me",
                headers={"Authorization": f"Bearer {access_t}"},
                timeout=10.0
            )
    except Exception as e:
        logger.error(f"Network error during heartbeat for {account_id}: {e}")
        return

    if resp.status_code == 403:
        # 403 = 账号被封禁/暂停，真正的封号信号
        await remove_account(account_id, reason="account banned (403 from /me)")
        return

    if resp.status_code == 401:
        # 401 = token 无效，但我们已经在上面刷新过了
        # 可能是刷新后的 token 还未同步，或者 session 被踢出
        # 不立即删号，由下一轮心跳配合 ensure_fresh_token 再判断
        logger.warning(f"Account {account_id} got 401 from /me after token refresh, will retry next cycle")
        return

    if resp.status_code != 200:
        logger.warning(f"Account {account_id} /me returned unexpected status {resp.status_code}, skipping")
        return

    # 账号正常，更新心跳时间
    # 配额信息（remaining/limit）由 client 在 checkin 时回传更新，heartbeat 不负责采集
    logger.debug(f"Account {account_id} heartbeat OK (status 200)")
    update_account(account_id, {"last_heartbeat_at": datetime.now(timezone.utc).isoformat()})

async def check_all_accounts():
    accounts = list_accounts()
    now = datetime.now(timezone.utc)
    
    for acc in accounts:
        account_id = acc["account_id"]
        status = acc["status"]
        last_hb_str = acc["last_heartbeat_at"]
        limit = acc["limit_requests"]
        remaining = acc["remaining_requests"]
        
        last_hb = None
        if last_hb_str:
            try:
                last_hb = datetime.fromisoformat(last_hb_str)
            except ValueError:
                pass
                
        elapsed_minutes = (now - last_hb).total_seconds() / 60 if last_hb else float('inf')
        
        if status == "READY":
            ratio = remaining / limit if limit > 0 else 1.0
            
            should_heartbeat = False
            if ratio > 0.8 and elapsed_minutes >= 30:
                should_heartbeat = True
            elif 0.3 <= ratio <= 0.8 and elapsed_minutes >= 15:
                should_heartbeat = True
            elif last_hb is None: # Never heartbeated
                should_heartbeat = True
                
            if should_heartbeat:
                await heartbeat_account(account_id)
                
        elif status == "COOLING":
            # For simplicity, we check if reset_at has passed or check once an hour to see if quota is back
            # Because reset_requests is like "2h47m30s", parsing it accurately is tricky.
            # We will just probe every 30 minutes in COOLING to see if it recovered.
            if elapsed_minutes >= 30:
                await heartbeat_account(account_id)
                
                # Check if it recovered
                updated_acc = get_account(account_id)
                if updated_acc:
                    n_limit = updated_acc["limit_requests"]
                    n_rem = updated_acc["remaining_requests"]
                    if n_limit > 0 and (n_rem / n_limit) >= 0.3:
                        update_account(account_id, {"status": "READY"})
                        logger.info(f"Account {account_id} recovered, switching to READY")

def start_scheduler():
    scheduler = AsyncIOScheduler()
    # Run check every 5 minutes
    scheduler.add_job(check_all_accounts, 'interval', minutes=5)
    scheduler.start()
    logger.info("Scheduler started")
