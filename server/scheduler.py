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

    # Call OpenAI API to check quota
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {auth['accessToken']}"},
                json={"model": "gpt-5.5", "input": "hi", "max_output_tokens": 1},
                timeout=10.0
            )
    except Exception as e:
        logger.error(f"Network error during heartbeat for {account_id}: {e}")
        return

    if resp.status_code == 401:
        await remove_account(account_id, reason="account banned")
        return

    # Update quota in DB
    update_data = {
        "limit_requests": int(resp.headers.get("x-ratelimit-limit-requests", -1)),
        "remaining_requests": int(resp.headers.get("x-ratelimit-remaining-requests", -1)),
        "reset_requests": resp.headers.get("x-ratelimit-reset-requests", ""),
        "limit_tokens": int(resp.headers.get("x-ratelimit-limit-tokens", -1)),
        "remaining_tokens": int(resp.headers.get("x-ratelimit-remaining-tokens", -1)),
        "last_heartbeat_at": datetime.now(timezone.utc).isoformat()
    }
    
    # Check if remaining is very low (<30%), if so change to COOLING
    limit = update_data["limit_requests"]
    remaining = update_data["remaining_requests"]
    
    if limit > 0 and remaining >= 0:
        ratio = remaining / limit
        if ratio < 0.3:
            update_data["status"] = "COOLING"
            logger.info(f"Account {account_id} quota low ({remaining}/{limit}), switching to COOLING")

    update_account(account_id, update_data)

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
