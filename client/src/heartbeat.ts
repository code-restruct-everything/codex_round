import axios from 'axios';
import * as vscode from 'vscode';
import { HttpsProxyAgent } from 'https-proxy-agent';
import { AuthJson } from './utils';

export interface QuotaInfo {
    banned: boolean;
    skipped?: boolean;
    reason?: string;
    limit_requests?: number;
    remaining_requests?: number;
    reset_requests?: string;
    limit_tokens?: number;
    remaining_tokens?: number;
}

export function isExpired(auth: AuthJson): boolean {
    const expiresAt = auth.expiresAt || 0;
    // Expired if current time is greater than expiresAt - 60 seconds
    return (Date.now() / 1000) + 60 >= expiresAt;
}

export async function runClientHeartbeat(auth: AuthJson): Promise<QuotaInfo> {
    // [核心防竞争] 闲置跳过
    if (isExpired(auth)) {
        return { skipped: true, reason: 'token_expired_idle', banned: false };
    }

    const config = vscode.workspace.getConfiguration('codexPool');
    const proxyUrl = config.get<string>('proxy', '');

    const axiosConfig: any = {
        timeout: 10000,
        headers: {
            'Authorization': `Bearer ${auth.accessToken || auth.access_token || (auth.tokens && auth.tokens.access_token)}`,
            'Content-Type': 'application/json'
        }
    };

    if (proxyUrl) {
        axiosConfig.httpsAgent = new HttpsProxyAgent(proxyUrl);
        axiosConfig.proxy = false; // Disable default axios proxy
    }

    try {
        // 探活用 chatgpt.com/backend-api/me，该端点接受 OAuth access_token
        // api.openai.com 要求 sk-xxx API Key，不能用 OAuth token探活
        const response = await axios.get(
            'https://chatgpt.com/backend-api/me',
            axiosConfig
        );

        // 200 = 账号正常；配额信息由 checkout/checkin 维护，heartbeat 不采集
        return {
            banned: false
        };

    } catch (e: any) {
        if (e.response && e.response.status === 403) {
            // 403 = 账号被封禁/暂停，真正的封号信号
            return { banned: true, reason: 'account_banned_403' };
        }
        if (e.response && e.response.status === 401) {
            // 401 不代表封号，可能是 token 过期或未同步
            // skipped = true 表示这轮不做任何处理，保持现有犰态
            console.warn('Heartbeat got 401 from /me, token may be stale, will retry on next cycle');
            return { banned: false, skipped: true, reason: 'token_stale_401' };
        }
        console.error('Heartbeat request failed:', e.message);
        return { banned: false, skipped: true, reason: 'network_error' };
    }
}
