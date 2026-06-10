"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.isExpired = isExpired;
exports.runClientHeartbeat = runClientHeartbeat;
const axios_1 = require("axios");
const vscode = require("vscode");
const https_proxy_agent_1 = require("https-proxy-agent");
function isExpired(auth) {
    const expiresAt = auth.expiresAt || 0;
    // Expired if current time is greater than expiresAt - 60 seconds
    return (Date.now() / 1000) + 60 >= expiresAt;
}
function getAccessToken(auth) {
    return auth.accessToken || auth.access_token || auth.tokens?.access_token || auth.tokens?.accessToken;
}
function getChatGptAccountId(auth) {
    return (auth.account_id ||
        auth.accountId ||
        auth.chatgpt_account_id ||
        auth.chatgptAccountId ||
        auth.tokens?.account_id ||
        auth.tokens?.accountId);
}
function toNumber(value) {
    if (value === null || value === undefined || typeof value === 'boolean') {
        return undefined;
    }
    if (typeof value === 'number') {
        return Number.isFinite(value) ? value : undefined;
    }
    if (typeof value === 'string') {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : undefined;
    }
    return undefined;
}
function toIsoFromEpoch(value) {
    let timestamp = toNumber(value);
    if (!timestamp || timestamp <= 0) {
        return undefined;
    }
    if (timestamp > 10000000000) {
        timestamp = timestamp / 1000;
    }
    return new Date(timestamp * 1000).toISOString();
}
function toIsoFromResetAfter(value) {
    const seconds = toNumber(value);
    if (seconds === undefined || seconds < 0) {
        return undefined;
    }
    return new Date(Date.now() + seconds * 1000).toISOString();
}
function normalizeWindow(window) {
    if (!window || typeof window !== 'object') {
        return undefined;
    }
    let percentLeft = toNumber(window.percent_left);
    if (percentLeft === undefined) {
        percentLeft = toNumber(window.remaining_percent);
    }
    if (percentLeft === undefined) {
        const usedPercent = toNumber(window.used_percent);
        if (usedPercent !== undefined) {
            percentLeft = 100 - usedPercent;
        }
    }
    if (percentLeft !== undefined) {
        percentLeft = Math.max(0, Math.min(100, percentLeft));
    }
    return {
        percentLeft,
        resetAt: toIsoFromEpoch(window.reset_time_ms) ||
            toIsoFromEpoch(window.reset_at) ||
            toIsoFromEpoch(window.resetsAt) ||
            toIsoFromResetAfter(window.reset_after_seconds),
        windowSeconds: toNumber(window.limit_window_seconds)
    };
}
function normalizeUsagePayload(payload) {
    const rateLimit = payload?.rate_limit || payload?.rate_limits || {};
    const candidates = [];
    for (const [source, raw] of [
        ['five_hour', payload?.five_hour || rateLimit?.five_hour],
        ['weekly', payload?.weekly || rateLimit?.weekly],
        ['primary', rateLimit?.primary_window || payload?.primary_window],
        ['secondary', rateLimit?.secondary_window || payload?.secondary_window]
    ]) {
        const window = normalizeWindow(raw);
        if (window) {
            candidates.push([source, window]);
        }
    }
    let fiveHour;
    let weekly;
    let primary;
    let secondary;
    for (const [source, window] of candidates) {
        if (source === 'five_hour' || window.windowSeconds === 18000) {
            fiveHour = window;
        }
        else if (source === 'weekly' || window.windowSeconds === 604800) {
            weekly = window;
        }
        else if (source === 'primary') {
            primary = window;
        }
        else if (source === 'secondary') {
            secondary = window;
        }
    }
    fiveHour = fiveHour || primary;
    weekly = weekly || secondary;
    const percentages = [fiveHour?.percentLeft, weekly?.percentLeft]
        .filter((value) => value !== undefined);
    const effectivePercentLeft = percentages.length ? Math.min(...percentages) : undefined;
    const resetParts = [];
    if (fiveHour?.resetAt) {
        resetParts.push(`5h ${fiveHour.resetAt}`);
    }
    if (weekly?.resetAt) {
        resetParts.push(`weekly ${weekly.resetAt}`);
    }
    return {
        banned: false,
        usage_source: 'wham_usage',
        usage_updated_at: new Date().toISOString(),
        usage_error: '',
        plan_type: payload?.plan_type,
        rate_limit_reached: Boolean(rateLimit?.limit_reached || payload?.limit_reached),
        five_hour_percent_left: fiveHour?.percentLeft,
        five_hour_reset_at: fiveHour?.resetAt,
        weekly_percent_left: weekly?.percentLeft,
        weekly_reset_at: weekly?.resetAt,
        limit_requests: effectivePercentLeft === undefined ? -1 : 100,
        remaining_requests: effectivePercentLeft === undefined ? -1 : Math.floor(effectivePercentLeft),
        reset_requests: resetParts.join(' | ')
    };
}
async function runClientHeartbeat(auth) {
    // [核心防竞争] 闲置跳过
    if (isExpired(auth)) {
        return { skipped: true, reason: 'token_expired_idle', banned: false };
    }
    const accessToken = getAccessToken(auth);
    if (!accessToken) {
        return { skipped: true, reason: 'missing_access_token', banned: false };
    }
    const config = vscode.workspace.getConfiguration('codexPool');
    const proxyUrl = config.get('proxy', '');
    const accountId = getChatGptAccountId(auth);
    const axiosConfig = {
        timeout: 10000,
        headers: {
            'Authorization': `Bearer ${accessToken}`,
            'Accept': 'application/json',
            'Origin': 'https://chatgpt.com'
        }
    };
    if (accountId) {
        axiosConfig.headers['ChatGPT-Account-Id'] = accountId;
    }
    if (proxyUrl) {
        axiosConfig.httpsAgent = new https_proxy_agent_1.HttpsProxyAgent(proxyUrl);
        axiosConfig.proxy = false; // Disable default axios proxy
    }
    try {
        const response = await axios_1.default.get('https://chatgpt.com/backend-api/wham/usage', axiosConfig);
        return normalizeUsagePayload(response.data);
    }
    catch (e) {
        if (e.response && e.response.status === 403) {
            // 403 = 账号被封禁/暂停，真正的封号信号
            return { banned: true, reason: 'account_banned_403' };
        }
        if (e.response && e.response.status === 401) {
            // 401 不代表封号，可能是 token 过期或未同步
            // skipped = true 表示这轮不做任何处理，保持现有犰态
            console.warn('Heartbeat got 401 from /wham/usage, token may be stale, will retry on next cycle');
            return { banned: false, skipped: true, reason: 'token_stale_401' };
        }
        console.error('Heartbeat request failed:', e.message);
        return { banned: false, skipped: true, reason: 'network_error' };
    }
}
//# sourceMappingURL=heartbeat.js.map