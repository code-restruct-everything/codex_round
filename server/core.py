import httpx
import json
import time
import os
import uuid
from pathlib import Path
from typing import Dict, Any, Optional
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

def atomic_write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

def backup_auth(account_id: str, suffix: str = "bak") -> Optional[Path]:
    path = get_auth_path(account_id)
    if not path.exists():
        return None
    safe_suffix = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in suffix)
    backup_path = path.with_name(f"{path.name}.{safe_suffix}.bak")
    atomic_write_bytes(backup_path, path.read_bytes())
    return backup_path

def save_auth(account_id: str, auth_data: Dict[str, Any]):
    path = get_auth_path(account_id)
    if path.exists():
        backup_auth(account_id)
    atomic_write_bytes(path, json.dumps(auth_data, indent=2).encode("utf-8"))

def is_expired(auth_data: Dict[str, Any]) -> bool:
    expires_at = auth_data.get("expiresAt")
    # expires_in from Codex CLI is a relative duration (e.g. 3600 seconds), not an absolute
    # timestamp. Only treat it as an absolute timestamp if it looks like a Unix epoch (> year 2001).
    if not isinstance(expires_at, (int, float)) or expires_at < 978307200:
        fallback = auth_data.get("expires_in")
        if isinstance(fallback, (int, float)) and fallback > 978307200:
            expires_at = fallback
        else:
            return True  # No valid absolute timestamp — force refresh
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

    if resp.status_code == 400:
        body = resp.text
        # 以下错误码均表示 session/token 已永久失效，无法通过重试恢复，需移除账号
        FATAL_CODES = ("invalid_grant", "app_session_terminated")
        if any(code in body for code in FATAL_CODES):
            logger.warning(f"Fatal OAuth error for account {account_id}: {body[:200]}")
            raise InvalidGrantError(f"session 已终止或 refresh_token 失效，需重新登录")

    if resp.status_code != 200:
        logger.error(f"OAuth endpoint error for {account_id}: {resp.status_code} - {resp.text}")
        raise NetworkError(f"OAuth endpoint error: {resp.status_code} - {resp.text}")

    logger.info(f"Successfully refreshed token for account {account_id}")
    data = resp.json()
    auth["access_token"] = data["access_token"]
    if "tokens" in auth:
        auth["tokens"]["access_token"] = data["access_token"]

    if "refresh_token" in data:
        auth["refresh_token"] = data["refresh_token"]
        if "tokens" in auth:
            auth["tokens"]["refresh_token"] = data["refresh_token"]

    auth["expiresAt"] = time.time() + data.get("expires_in", 3600)
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat()

    save_auth(account_id, auth)
    return auth

async def ensure_fresh_token(account_id: str) -> Dict[str, Any]:
    auth = load_auth(account_id)
    if is_expired(auth):
        auth = await refresh_token(account_id)
    return auth
