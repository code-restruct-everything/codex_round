from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Dict, Any, List
from datetime import datetime, timezone
import os
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("vault.main")

from db import (
    get_account, list_accounts, insert_account, delete_account,
    update_account, pick_best_ready_account, db_lock
)
from core import ensure_fresh_token, save_auth, get_auth_path, InvalidGrantError
from scheduler import start_scheduler, remove_account

# API Key config
VAULT_API_KEY = os.environ.get("VAULT_API_KEY", "default_secret_key_change_me")

app = FastAPI(title="Codex Account Pool Vault")
security = HTTPBearer()

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
    remaining_requests: int
    limit_requests: int
    reset_requests: str

@app.post("/checkout", dependencies=[Depends(verify_api_key)])
async def checkout():
    with db_lock:
        best_acc = pick_best_ready_account()
        if not best_acc:
            logger.warning("Checkout requested but no READY accounts available.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No accounts available currently."
            )
        
        account_id = best_acc["account_id"]
        logger.info(f"Checking out account {account_id}")
        
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
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error refreshing token: {str(e)}"
            )
        
        # Update status to IN_USE
        update_account(account_id, {
            "status": "IN_USE",
            "checked_out_at": datetime.now(timezone.utc).isoformat()
        })
        
        return {
            "account_id": account_id,
            "auth_json": auth_data,
            "remaining_requests": best_acc["remaining_requests"],
            "limit_requests": best_acc["limit_requests"],
            "reset_requests": best_acc["reset_requests"] or ""
        }

class CheckinRequest(BaseModel):
    auth_json: Dict[str, Any]

@app.post("/checkin/{account_id}", dependencies=[Depends(verify_api_key)])
async def checkin(account_id: str, req: CheckinRequest):
    logger.info(f"Checking in account {account_id}")
    acc = get_account(account_id)
    if not acc:
        logger.warning(f"Checkin failed: Account {account_id} not found")
        raise HTTPException(status_code=404, detail="Account not found")
        
    # Save the auth_json provided by client
    save_auth(account_id, req.auth_json)
    
    # Refresh to ensure it's fresh in our DB
    try:
        await ensure_fresh_token(account_id)
    except Exception as e:
        # If it fails, we still keep it, just log it. Client returned it anyway.
        logger.warning(f"Could not refresh token on checkin for {account_id}: {e}")
        
    update_account(account_id, {
        "status": "READY",
        "returned_at": datetime.now(timezone.utc).isoformat()
    })
    
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
