import axios, { AxiosInstance } from 'axios';
import * as vscode from 'vscode';
import { AuthJson } from './utils';

function getVaultClient(): AxiosInstance {
    const config = vscode.workspace.getConfiguration('codexPool');
    const vaultUrl = config.get<string>('vaultUrl', 'http://127.0.0.1:8000');
    const apiKey = config.get<string>('apiKey', '');

    return axios.create({
        baseURL: vaultUrl,
        headers: {
            'Authorization': `Bearer ${apiKey}`,
            'Content-Type': 'application/json'
        },
        timeout: 15000
    });
}

export interface CheckoutResult {
    account_id: string;
    auth_json: AuthJson;
    remaining_requests: number;
    limit_requests: number;
    reset_requests: string;
}

export async function checkoutAccount(): Promise<CheckoutResult | null> {
    const client = getVaultClient();
    try {
        const response = await client.post('/checkout');
        return response.data as CheckoutResult;
    } catch (e: any) {
        if (e.response && e.response.status === 503) {
            vscode.window.showWarningMessage(`Codex Pool: ${e.response.data.detail}`);
            return null;
        }
        console.error('Checkout failed:', e);
        vscode.window.showErrorMessage(`Codex Pool Checkout Failed: ${e.message}`);
        throw e;
    }
}

export async function checkinAccount(accountId: string, authJson: AuthJson): Promise<boolean> {
    const client = getVaultClient();
    try {
        await client.post(`/checkin/${accountId}`, { auth_json: authJson });
        return true;
    } catch (e: any) {
        console.error('Checkin failed:', e);
        vscode.window.showErrorMessage(`Codex Pool Checkin Failed: ${e.message}`);
        // We throw so caller can implement exponential backoff
        throw e;
    }
}

export async function deleteAccount(accountId: string): Promise<boolean> {
    const client = getVaultClient();
    try {
        await client.delete(`/accounts/${accountId}`);
        return true;
    } catch (e: any) {
        console.error('Delete account failed:', e);
        return false;
    }
}
