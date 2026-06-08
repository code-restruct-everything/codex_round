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
        const response = await axios.post(
            'https://api.openai.com/v1/responses',
            {
                model: 'gpt-5.5',
                input: 'hi',
                max_output_tokens: 1
            },
            axiosConfig
        );

        return {
            banned: false,
            limit_requests: parseInt(response.headers['x-ratelimit-limit-requests'] || '-1', 10),
            remaining_requests: parseInt(response.headers['x-ratelimit-remaining-requests'] || '-1', 10),
            reset_requests: response.headers['x-ratelimit-reset-requests'] || '',
            limit_tokens: parseInt(response.headers['x-ratelimit-limit-tokens'] || '-1', 10),
            remaining_tokens: parseInt(response.headers['x-ratelimit-remaining-tokens'] || '-1', 10)
        };

    } catch (e: any) {
        if (e.response && e.response.status === 401) {
            return { banned: true };
        }
        console.error('Heartbeat request failed:', e.message);
        // On network error, we don't assume banned, just return current state loosely
        return { banned: false, skipped: true, reason: 'network_error' };
    }
}
