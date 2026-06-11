import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from db import list_accounts, update_account, delete_account, get_account
from core import ensure_fresh_token, InvalidGrantError, get_auth_path
from usage import fetch_usage_updates, InvalidUsageAccountError

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

    try:
        updates = await fetch_usage_updates(auth)
    except InvalidUsageAccountError:
        await remove_account(account_id, reason="account banned (403 from /wham/usage)")
        return
    except PermissionError:
        logger.warning(f"Account {account_id} got 401 from /wham/usage after token refresh, will retry next cycle")
        update_account(account_id, {
            "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "usage_error": "usage_401_after_refresh",
        })
        return
    except Exception as e:
        logger.error(f"Usage fetch error during heartbeat for {account_id}: {e}")
        update_account(account_id, {
            "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "usage_error": str(e)[:500],
        })
        return

    updates["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    remaining = updates.get("remaining_pct")
    limit = updates.get("limit_pct")
    if updates.get("rate_limit_reached") or remaining == 0 or (limit and limit > 0 and remaining is not None and remaining >= 0 and (remaining / limit) < 0.3):
        updates["status"] = "COOLING"
    logger.debug(f"Account {account_id} usage heartbeat OK")
    update_account(account_id, updates)

async def check_all_accounts():
    accounts = list_accounts()
    now = datetime.now(timezone.utc)
    
    for acc in accounts:
        account_id = acc["account_id"]
        status = acc["status"]
        last_hb_str = acc["last_heartbeat_at"]
        limit = acc["limit_pct"]
        remaining = acc["remaining_pct"]
        
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
            if last_hb is None:  # Never heartbeated
                should_heartbeat = True
            elif ratio > 0.8 and elapsed_minutes >= 30:
                should_heartbeat = True
            elif 0.3 <= ratio <= 0.8 and elapsed_minutes >= 15:
                should_heartbeat = True
            elif ratio < 0.3 and elapsed_minutes >= 5:
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
                    n_limit = updated_acc["limit_pct"]
                    n_rem = updated_acc["remaining_pct"]
                    if n_limit > 0 and (n_rem / n_limit) >= 0.3 and not updated_acc["rate_limit_reached"]:
                        update_account(account_id, {"status": "READY"})
                        logger.info(f"Account {account_id} recovered, switching to READY")

def start_scheduler():
    scheduler = AsyncIOScheduler()
    # Run check every 5 minutes
    scheduler.add_job(check_all_accounts, 'interval', minutes=5)
    scheduler.start()
    logger.info("Scheduler started")
