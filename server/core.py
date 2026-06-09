import httpx
import json
import time
import os
from pathlib import Path
from typing import Dict, Any
from datetime import datetime, timezone
import logging

logger = logging.getLogger("vault.core")

class InvalidGrantError(Exception): pass
class NetworkError(Exception): pass

# Base path for storing auth.json files
ACCOUNTS_DIR = Path(__file__).parent.parent / "vault" / "accounts"
ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

# The correct OAuth client_id used by the OpenAI Codex desktop/CLI app.
# Extracted from the official codex.exe binary: app_EMoamEEZ73f0CkXaXp7hrann
# The old UUID (1a314b17-72ee-4836-96b0-73f1d8cce4c8) is invalid and causes 401 invalid_client errors.
CLIENT_ID = os.environ.get("OPENAI_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")

def get_auth_path(account_id: str) -> Path:
    return ACCOUNTS_DIR / account_id / "auth.json"

def load_auth(account_id: str) -> Dict[str, Any]:
    path = get_auth_path(account_id)
    if not path.exists():
        raise FileNotFoundError(f"Auth file not found for account: {account_id}")
    return json.loads(path.read_text(encoding="utf-8"))

def save_auth(account_id: str, auth_data: Dict[str, Any]):
    path = get_auth_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(auth_data, indent=2), encoding="utf-8")

def is_expired(auth_data: Dict[str, Any]) -> bool:
    # Check if access token is expired or about to expire in 60 seconds
    expires_at = auth_data.get("expiresAt") or auth_data.get("expires_in")
    if not isinstance(expires_at, (int, float)):
        return True  # Force refresh if no numeric expiration found
    return time.time() + 60 >= expires_at

async def refresh_token(account_id: str) -> Dict[str, Any]:
    auth = load_auth(account_id)

    rt = auth.get("refresh_token") or auth.get("refreshToken")
    if not rt and "tokens" in auth:
        rt = auth["tokens"].get("refresh_token")
    if not rt:
        logger.error(f"No refresh_token found in auth data for account {account_id}")
        raise InvalidGrantError("No refresh_token found in auth data")

    logger.debug(f"Attempting to refresh token for account {account_id}")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://auth.openai.com/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                    "client_id": CLIENT_ID
                },
                timeout=10.0
            )
    except httpx.TimeoutException:
        logger.warning(f"Timeout while refreshing token for {account_id}")
        raise NetworkError("OAuth 端点超时，非封号，稍后重试")
    except httpx.RequestError as e:
        logger.error(f"Network error refreshing token for {account_id}: {e}")
        raise NetworkError(f"网络错误：{e}")

    if resp.status_code == 400 and "invalid_grant" in resp.text:
        logger.warning(f"invalid_grant received for account {account_id}")
        raise InvalidGrantError("refresh_token 已失效，账号被封或长期未用")
    
    if resp.status_code != 200:
        logger.error(f"OAuth endpoint error for {account_id}: {resp.status_code} - {resp.text}")
        raise NetworkError(f"OAuth endpoint error: {resp.status_code} - {resp.text}")

    logger.info(f"Successfully refreshed token for account {account_id}")
    data = resp.json()
    auth["access_token"] = data["access_token"]
    # Provide camelCase too just in case older clients expect it
    auth["accessToken"] = data["access_token"]
    if "tokens" in auth:
        auth["tokens"]["access_token"] = data["access_token"]
    
    # Rotation: check if a new refresh token is provided
    if "refresh_token" in data:
        auth["refresh_token"] = data["refresh_token"]
        auth["refreshToken"] = data["refresh_token"]
        if "tokens" in auth:
            auth["tokens"]["refresh_token"] = data["refresh_token"]
        
    auth["expiresAt"] = time.time() + data.get("expires_in", 3600)
    auth["expires_in"] = time.time() + data.get("expires_in", 3600)
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat()

    save_auth(account_id, auth)
    return auth

async def ensure_fresh_token(account_id: str) -> Dict[str, Any]:
    auth = load_auth(account_id)
    if is_expired(auth):
        auth = await refresh_token(account_id)
    return auth
