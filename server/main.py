from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import asyncio
import os
import logging

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("vault.main")

from db import (
    get_account, list_accounts, insert_account, delete_account,
    update_account, pick_best_ready_account
)
from core import ensure_fresh_token, save_auth, get_auth_path, InvalidGrantError
from scheduler import start_scheduler, remove_account, checkin_account, get_checkin_lock

# API Key config
VAULT_API_KEY = os.environ.get("VAULT_API_KEY", "default_secret_key_change_me")

app = FastAPI(title="Codex Account Pool Vault")
security = HTTPBearer()
checkout_lock = asyncio.Lock()

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != VAULT_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )
    return credentials.credentials

@app.on_event("startup")
async def startup_event():
    logger.info("Starting up Vault server...")
    start_scheduler()

@app.get("/status", dependencies=[Depends(verify_api_key)])
async def get_status():
    accounts = list_accounts()
    result = []
    for acc in accounts:
        result.append(dict(acc))
    return {"accounts": result}

class CheckoutResponse(BaseModel):
    account_id: str
    auth_json: Dict[str, Any]
    checkout_request_id: str
    remaining_pct: int
    limit_pct: int
    reset_requests: str
    five_hour_percent_left: Optional[float] = None
    five_hour_reset_at: Optional[str] = None
    weekly_percent_left: Optional[float] = None
    weekly_reset_at: Optional[str] = None
    usage_updated_at: Optional[str] = None
    usage_source: Optional[str] = None

class CheckoutRequest(BaseModel):
    checkout_request_id: str

def find_checkout_request(checkout_request_id: str):
    for acc in list_accounts():
        if acc["checkout_request_id"] == checkout_request_id and acc["status"] in ("CHECKING_OUT", "IN_USE"):
            return acc
    return None

@app.post("/checkout", dependencies=[Depends(verify_api_key)])
async def checkout(req: CheckoutRequest):
    # asyncio.Lock prevents two coroutines from double-checking out the same account.
    # threading.RLock is reentrant within the same thread, so it does NOT block concurrent
    # async coroutines that share the same event-loop thread.
    async with checkout_lock:
        best_acc = find_checkout_request(req.checkout_request_id)
        if best_acc:
            logger.info(f"Replaying checkout request {req.checkout_request_id} for account {best_acc['account_id']}")
        else:
            best_acc = pick_best_ready_account()
        if not best_acc:
            logger.warning("Checkout requested but no READY accounts available.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No accounts available currently."
            )

        account_id = best_acc["account_id"]
        logger.info(f"Checking out account {account_id}")

        if best_acc["status"] == "READY":
            # Mark CHECKING_OUT before any await so no other coroutine can pick this account.
            update_account(account_id, {
                "status": "CHECKING_OUT",
                "checkout_request_id": req.checkout_request_id,
                "checkout_acknowledged_at": None,
                "checked_out_at": datetime.now(timezone.utc).isoformat()
            })

    # Token refresh happens outside the global checkout lock, but inside the per-account
    # lock so checkout, checkin finalize, and scheduler heartbeat cannot refresh together.
    async with get_checkin_lock(account_id):
        latest_acc = get_account(account_id)
        if not latest_acc or latest_acc["checkout_request_id"] != req.checkout_request_id:
            raise HTTPException(status_code=409, detail="Checkout request no longer owns this account.")
        try:
            auth_data = await ensure_fresh_token(account_id)
        except InvalidGrantError:
            logger.warning(f"Account {account_id} refresh_token invalid during checkout")
            await remove_account(account_id, "refresh_token invalid during checkout")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Selected account became invalid. Please try again."
            )
        except Exception as e:
            logger.error(f"Error refreshing token for {account_id} during checkout: {e}", exc_info=True)
            if latest_acc["status"] == "CHECKING_OUT":
                update_account(account_id, {
                    "status": "READY",
                    "checkout_request_id": None,
                    "checkout_acknowledged_at": None
                })
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error refreshing token: {str(e)}"
            )

    response_acc = get_account(account_id) or best_acc

    return {
        "account_id": account_id,
        "auth_json": auth_data,
        "checkout_request_id": req.checkout_request_id,
        "remaining_pct": response_acc["remaining_pct"],
        "limit_pct": response_acc["limit_pct"],
        "reset_requests": response_acc["reset_requests"] or "",
        "five_hour_percent_left": response_acc["five_hour_percent_left"],
        "five_hour_reset_at": response_acc["five_hour_reset_at"],
        "weekly_percent_left": response_acc["weekly_percent_left"],
        "weekly_reset_at": response_acc["weekly_reset_at"],
        "usage_updated_at": response_acc["usage_updated_at"],
        "usage_source": response_acc["usage_source"],
    }

class CheckoutAckRequest(BaseModel):
    checkout_request_id: str

@app.post("/checkout/{account_id}/ack", dependencies=[Depends(verify_api_key)])
async def checkout_ack(account_id: str, req: CheckoutAckRequest):
    async with get_checkin_lock(account_id):
        acc = get_account(account_id)
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found")
        if acc["checkout_request_id"] != req.checkout_request_id:
            raise HTTPException(status_code=409, detail="Checkout request does not match account.")
        if acc["status"] == "CHECKING_OUT":
            update_account(account_id, {
                "status": "IN_USE",
                "checkout_acknowledged_at": datetime.now(timezone.utc).isoformat()
            })
            return {"status": "ok"}
        if acc["status"] == "IN_USE":
            return {"status": "ok", "already_acknowledged": True}
        raise HTTPException(status_code=409, detail=f"Cannot ack checkout while account is {acc['status']}.")

class CheckinRequest(BaseModel):
    auth_json: Dict[str, Any]

@app.post("/checkin/{account_id}", dependencies=[Depends(verify_api_key)])
async def checkin(account_id: str, req: CheckinRequest):
    logger.info(f"Checking in account {account_id}")
    try:
        return await checkin_account(account_id, req.auth_json)
    except KeyError:
        logger.warning(f"Checkin failed: Account {account_id} not found")
        raise HTTPException(status_code=404, detail="Account not found")

class UsageUpdateRequest(BaseModel):
    remaining_pct: Optional[int] = None
    limit_pct: Optional[int] = None
    reset_requests: Optional[str] = None
    limit_tokens: Optional[int] = None
    remaining_tokens: Optional[int] = None
    five_hour_percent_left: Optional[float] = None
    five_hour_reset_at: Optional[str] = None
    weekly_percent_left: Optional[float] = None
    weekly_reset_at: Optional[str] = None
    usage_updated_at: Optional[str] = None
    usage_source: Optional[str] = None
    usage_error: Optional[str] = None
    plan_type: Optional[str] = None
    rate_limit_reached: Optional[bool] = None

@app.post("/accounts/{account_id}/usage", dependencies=[Depends(verify_api_key)])
async def update_usage(account_id: str, req: UsageUpdateRequest):
    logger.info(f"Updating usage for account {account_id}")
    acc = get_account(account_id)
    if not acc:
        logger.warning(f"Usage update failed: Account {account_id} not found")
        raise HTTPException(status_code=404, detail="Account not found")

    updates = {}
    for field, value in req.model_dump(exclude_unset=True).items():
        if value is not None:
            updates[field] = value

    if updates and "usage_updated_at" not in updates:
        updates["usage_updated_at"] = datetime.now(timezone.utc).isoformat()

    if acc["status"] not in ("IN_USE", "RETURNING", "CHECKING_OUT") and (updates.get("rate_limit_reached") or updates.get("remaining_pct") == 0):
        updates["status"] = "COOLING"

    update_account(account_id, updates)
    return {"status": "ok"}

@app.delete("/accounts/{account_id}", dependencies=[Depends(verify_api_key)])
async def delete_acc(account_id: str):
    logger.info(f"Deleting account {account_id} via API")
    await remove_account(account_id, "Deleted via API")
    return {"status": "ok"}

class AddAccountRequest(BaseModel):
    account_id: str
    auth_json: Dict[str, Any]

@app.post("/accounts", dependencies=[Depends(verify_api_key)])
async def add_account(req: AddAccountRequest):
    # Ensure it's valid, checking both flat and nested 'tokens' structure
    auth = req.auth_json
    has_access = "access_token" in auth or "accessToken" in auth or ("tokens" in auth and "access_token" in auth["tokens"])
    has_refresh = "refresh_token" in auth or "refreshToken" in auth or ("tokens" in auth and "refresh_token" in auth["tokens"])
    if not has_access or not has_refresh:
        logger.warning(f"Failed to add account {req.account_id}: Invalid auth.json format")
        raise HTTPException(status_code=400, detail="Invalid auth.json format")
        
    save_auth(req.account_id, req.auth_json)
    insert_account(req.account_id)
    logger.info(f"Successfully added account {req.account_id}")
    return {"status": "ok", "account_id": req.account_id}
