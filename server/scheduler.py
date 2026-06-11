import logging
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from db import list_accounts, update_account, get_account
from core import ensure_fresh_token, InvalidGrantError, save_auth, backup_auth
from usage import fetch_usage_updates, InvalidUsageAccountError

logger = logging.getLogger("vault.scheduler")
checkin_locks: Dict[str, asyncio.Lock] = {}

class LeaseMismatchError(Exception):
    pass

def get_checkin_lock(account_id: str) -> asyncio.Lock:
    lock = checkin_locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        checkin_locks[account_id] = lock
    return lock

async def remove_account(account_id: str, reason: str):
    logger.warning(f"⚠️ 账号 {account_id} 已隔离（原因：{reason}）")
    backup_path = backup_auth(account_id, "quarantine")
    updates = {
        "status": "QUARANTINED",
        "is_healthy": 0,
        "quarantine_reason": reason,
        "quarantined_at": datetime.now(timezone.utc).isoformat(),
        "checkout_request_id": None,
        "checkout_acknowledged_at": None,
        "usage_error": reason[:500],
    }
    update_account(account_id, updates)
    if backup_path:
        logger.warning(f"Auth backup retained for {account_id}: {backup_path}")
    # TODO: Send alert via email/slack

async def checkin_account(account_id: str, auth_json: Dict[str, Any], checkout_request_id: str):
    async with get_checkin_lock(account_id):
        acc = get_account(account_id)
        if not acc:
            logger.warning(f"Checkin failed: Account {account_id} not found")
            raise KeyError(account_id)

        if acc["status"] in ("IN_USE", "RETURNING"):
            stored_request_id = acc["checkout_request_id"]
            if stored_request_id and stored_request_id != checkout_request_id:
                raise LeaseMismatchError("Checkin lease does not match account owner.")

        if acc["status"] != "IN_USE":
            logger.info(f"Account {account_id} already checked in with status {acc['status']}")
            if acc["status"] == "RETURNING":
                return await finalize_checkin_locked(account_id)
            return {"status": "ok", "already_checked_in": True}

        save_auth(account_id, auth_json)
        update_account(account_id, {
            "status": "RETURNING",
            "returned_at": datetime.now(timezone.utc).isoformat()
        })

        return await finalize_checkin_locked(account_id)

async def finalize_checkin(account_id: str):
    async with get_checkin_lock(account_id):
        return await finalize_checkin_locked(account_id)

async def finalize_checkin_locked(account_id: str):
    acc = get_account(account_id)
    if not acc:
        logger.warning(f"Could not finalize checkin for missing account {account_id}")
        return {"status": "missing"}
    if acc["status"] != "RETURNING":
        logger.info(f"Account {account_id} does not need RETURNING recovery; status={acc['status']}")
        return {"status": "ok", "already_checked_in": True}

    try:
        await ensure_fresh_token(account_id)
    except InvalidGrantError:
        await remove_account(account_id, reason="refresh_token invalid on checkin")
        return {"status": "quarantined"}
    except Exception as e:
        logger.warning(f"Could not refresh token on checkin for {account_id}: {e}")
        raise

    latest_acc = get_account(account_id)
    next_status = "READY"
    if latest_acc:
        limit_pct = latest_acc["limit_pct"]
        remaining_pct = latest_acc["remaining_pct"]
        if latest_acc["rate_limit_reached"] or (limit_pct > 0 and remaining_pct >= 0 and (remaining_pct / limit_pct) < 0.3):
            next_status = "COOLING"

    update_account(account_id, {
        "status": next_status,
        "checkout_request_id": None,
        "checkout_acknowledged_at": None
    })
    logger.info(f"Account {account_id} finalized checkin as {next_status}")
    return {"status": "ok"}

async def recover_returning_accounts():
    accounts = list_accounts()
    for acc in accounts:
        if acc["status"] == "RETURNING":
            logger.info(f"Recovering RETURNING account {acc['account_id']}")
            try:
                await finalize_checkin(acc["account_id"])
            except Exception as e:
                logger.warning(f"RETURNING recovery will retry for {acc['account_id']}: {e}")

async def heartbeat_account(account_id: str):
    logger.info(f"Running heartbeat for account {account_id}")
    async with get_checkin_lock(account_id):
        acc = get_account(account_id)
        if not acc or acc["status"] not in ("READY", "COOLING"):
            logger.info(f"Skipping heartbeat for {account_id}; status changed to {acc['status'] if acc else 'missing'}")
            return

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
            latest_acc = get_account(account_id)
            if latest_acc and latest_acc["status"] in ("READY", "COOLING"):
                update_account(account_id, {
                    "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "usage_error": "usage_401_after_refresh",
                })
            return
        except Exception as e:
            logger.error(f"Usage fetch error during heartbeat for {account_id}: {e}")
            latest_acc = get_account(account_id)
            if latest_acc and latest_acc["status"] in ("READY", "COOLING"):
                update_account(account_id, {
                    "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "usage_error": str(e)[:500],
                })
            return

        latest_acc = get_account(account_id)
        if not latest_acc or latest_acc["status"] not in ("READY", "COOLING"):
            logger.info(f"Skipping heartbeat update for {account_id}; status changed to {latest_acc['status'] if latest_acc else 'missing'}")
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
        
        if status == "RETURNING":
            try:
                await finalize_checkin(account_id)
            except Exception as e:
                logger.warning(f"RETURNING recovery will retry for {account_id}: {e}")

        elif status == "READY":
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
    scheduler.add_job(
        recover_returning_accounts,
        'date',
        run_date=datetime.now(timezone.utc),
        id='recover_returning_accounts_startup',
        replace_existing=True
    )
    scheduler.add_job(
        recover_returning_accounts,
        'interval',
        minutes=1,
        id='recover_returning_accounts',
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )
    # Run check every 5 minutes
    scheduler.add_job(
        check_all_accounts,
        'interval',
        minutes=5,
        id='check_all_accounts',
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )
    scheduler.start()
    logger.info("Scheduler started")
