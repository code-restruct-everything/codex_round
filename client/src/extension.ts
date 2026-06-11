import * as vscode from 'vscode';
import { randomUUID } from 'crypto';
import { readClientState, writeClientState, readAuthJson, writeAuthJson, clearClientState } from './utils';
import { ackCheckout, checkoutAccount, checkinAccount, deleteAccount, updateAccountUsage, CheckoutResult } from './vaultApi';
import { runClientHeartbeat, QuotaInfo } from './heartbeat';

let heartbeatTimer: NodeJS.Timeout | undefined;
let statusBarItem: vscode.StatusBarItem;
let currentAccountId: string | undefined;
const CHECKIN_RETRY_DELAYS_MS = [5_000, 15_000, 30_000, 60_000, 120_000];
const CHECKOUT_RETRY_DELAYS_MS = [5_000, 15_000, 30_000, 60_000, 120_000];

export async function activate(context: vscode.ExtensionContext) {
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'codexPool.showPanel';
    context.subscriptions.push(statusBarItem);
    statusBarItem.show();

    context.subscriptions.push(vscode.commands.registerCommand('codexPool.returnAccount', async () => {
        await manualReturn();
    }));

    context.subscriptions.push(vscode.commands.registerCommand('codexPool.showPanel', () => {
        vscode.window.showInformationMessage(`Codex Pool Status: Current Account - ${currentAccountId || 'None'}`);
    }));

    const state = readClientState();
    if (state?.pending_checkin_account_id) {
        currentAccountId = state.pending_checkin_account_id;
        updateStatusBar({ account_id: currentAccountId, status: '恢复归还中...' });
        stopHeartbeatTimer();
        await checkinCurrentAccountWithRetry(currentAccountId);
        clearClientState();
        await performCheckout();
    } else if (state?.current_account_id) {
        currentAccountId = state.current_account_id;
        updateStatusBar({ account_id: currentAccountId, status: '恢复中...' });
    } else {
        await performCheckout();
    }

    if (currentAccountId) {
        const quota = await performHeartbeat();
        if (quota && !quota.banned) {
            scheduleNextHeartbeat(quota.remaining_pct || -1, quota.limit_pct || -1);
        }
    }
}

async function performCheckout() {
    updateStatusBar({ status: '获取账号中...' });
    const state = readClientState();
    const checkoutRequestId = state?.checkout_request_id || randomUUID();
    const checkoutStartedAt = state?.checkout_started_at || new Date().toISOString();
    let result: CheckoutResult | null = null;

    writeClientState({
        checkout_request_id: checkoutRequestId,
        checkout_started_at: checkoutStartedAt,
        pending_checkout_account_id: state?.pending_checkout_account_id
    });

    if (state?.pending_checkout_account_id && readAuthJson()) {
        currentAccountId = state.pending_checkout_account_id;
        updateStatusBar({ status: '确认账号中...' });
    } else {
        result = await checkoutAccountWithRetry(checkoutRequestId);
        currentAccountId = result.account_id;
        writeAuthJson(result.auth_json);
        writeClientState({
            checkout_request_id: checkoutRequestId,
            checkout_started_at: checkoutStartedAt,
            pending_checkout_account_id: currentAccountId
        });
        updateStatusBar({
            account_id: currentAccountId,
            remaining_pct: result.remaining_pct,
            limit_pct: result.limit_pct,
            reset_requests: result.reset_requests,
            five_hour_percent_left: result.five_hour_percent_left,
            five_hour_reset_at: result.five_hour_reset_at,
            weekly_percent_left: result.weekly_percent_left,
            weekly_reset_at: result.weekly_reset_at
        });
    }

    await ackCheckoutWithRetry(currentAccountId!, checkoutRequestId);
    writeClientState({
        current_account_id: currentAccountId,
        checked_out_at: new Date().toISOString(),
        checkout_request_id: checkoutRequestId,
        checkout_started_at: checkoutStartedAt
    });
}

async function performHeartbeat(): Promise<QuotaInfo | null> {
    if (!currentAccountId) return null;
    
    const auth = readAuthJson();
    if (!auth) {
        vscode.window.showErrorMessage("本地 auth.json 丢失，请重新分配账号。");
        return null;
    }

    const quota = await runClientHeartbeat(auth);

    if (quota.banned) {
        vscode.window.showErrorMessage(`账号 ${currentAccountId} 已失效或被封禁，正在切换...`);
        stopHeartbeatTimer();
        const deleted = await deleteAccount(currentAccountId);
        if (!deleted) {
            updateStatusBar({ account_id: currentAccountId, status: '隔离失败，停止切换' });
            return null;
        }
        clearClientState();
        await performCheckout();
        return null;
    }

    if (!quota.skipped) {
        const { banned, skipped, reason, ...usageUpdate } = quota;
        await updateAccountUsage(currentAccountId, usageUpdate);

        updateStatusBar({
            account_id: currentAccountId,
            remaining_pct: quota.remaining_pct,
            limit_pct: quota.limit_pct,
            reset_requests: quota.reset_requests,
            five_hour_percent_left: quota.five_hour_percent_left,
            five_hour_reset_at: quota.five_hour_reset_at,
            weekly_percent_left: quota.weekly_percent_left,
            weekly_reset_at: quota.weekly_reset_at
        });

        // 自动换号逻辑：剩余额度极低
        if (quota.rate_limit_reached || (quota.remaining_pct !== undefined && quota.remaining_pct < 5 && quota.limit_pct !== -1)) {
            const accountToReturn = currentAccountId;
            vscode.window.showInformationMessage(`账号 ${currentAccountId} 额度即将耗尽，正在切换...`);
            markCheckinPending(accountToReturn);
            stopHeartbeatTimer();
            await checkinCurrentAccountWithRetry(accountToReturn);
            clearClientState();
            await performCheckout();
            return null;
        }
    }

    return quota;
}

function scheduleNextHeartbeat(remaining: number, limit: number) {
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
    
    let intervalMs = 5 * 60_000; // 5 min default
    if (limit > 0 && remaining >= 0) {
        const ratio = remaining / limit;
        if (ratio > 0.8) intervalMs = 30 * 60_000;
        else if (ratio > 0.3) intervalMs = 15 * 60_000;
    }

    heartbeatTimer = setTimeout(async () => {
        const quota = await performHeartbeat();
        if (quota && !quota.banned) {
            scheduleNextHeartbeat(quota.remaining_pct || -1, quota.limit_pct || -1);
        }
    }, intervalMs);
}

async function checkoutAccountWithRetry(checkoutRequestId: string): Promise<CheckoutResult> {
    let attempt = 0;

    while (true) {
        try {
            const result = await checkoutAccount(checkoutRequestId, false);
            if (result) {
                return result;
            }
        } catch (e: any) {
            console.error(`Checkout attempt ${attempt + 1} failed`, e.message);
        }

        attempt += 1;
        const delayMs = getCheckoutRetryDelay(attempt);
        updateStatusBar({ status: `获取账号失败，${Math.round(delayMs / 1000)}秒后重试...` });
        await sleep(delayMs);
    }
}

async function ackCheckoutWithRetry(accountId: string, checkoutRequestId: string): Promise<void> {
    let attempt = 0;

    while (true) {
        try {
            await ackCheckout(accountId, checkoutRequestId, false);
            return;
        } catch (e: any) {
            attempt += 1;
            const delayMs = getCheckoutRetryDelay(attempt);
            console.error(`Checkout ack attempt ${attempt} failed for ${accountId}`, e.message);
            updateStatusBar({ status: `确认账号失败，${Math.round(delayMs / 1000)}秒后重试...` });
            await sleep(delayMs);
        }
    }
}

function stopHeartbeatTimer() {
    if (heartbeatTimer) {
        clearTimeout(heartbeatTimer);
        heartbeatTimer = undefined;
    }
}

async function checkinCurrentAccountWithRetry(accountId: string): Promise<void> {
    let attempt = 0;
    const state = readClientState();
    const checkoutRequestId = state?.pending_checkin_checkout_request_id || state?.checkout_request_id;
    if (!checkoutRequestId) {
        updateStatusBar({ account_id: accountId, status: '租约缺失，停止归还' });
        throw new Error(`Missing checkout_request_id for account ${accountId}; refusing unsafe checkin.`);
    }

    while (true) {
        const latestAuth = readAuthJson();
        if (latestAuth) {
            try {
                await checkinAccount(accountId, latestAuth, checkoutRequestId, false);
                vscode.window.showInformationMessage(`成功归还账号: ${accountId}`);
                return;
            } catch (e: any) {
                attempt += 1;
                const delayMs = getCheckinRetryDelay(attempt);
                console.error(`Checkin attempt ${attempt} failed for ${accountId}`, e.message);
                updateStatusBar({ status: `归还失败，${Math.round(delayMs / 1000)}秒后重试...` });
                await sleep(delayMs);
                continue;
            }
        }

        attempt += 1;
        const delayMs = getCheckinRetryDelay(attempt);
        updateStatusBar({ status: `auth.json 缺失，${Math.round(delayMs / 1000)}秒后重试...` });
        await sleep(delayMs);
    }
}

function markCheckinPending(accountId: string): void {
    const state = readClientState();
    const checkoutRequestId = state?.checkout_request_id;
    if (!checkoutRequestId) {
        throw new Error(`Missing checkout_request_id for account ${accountId}; refusing unsafe checkin.`);
    }

    writeClientState({
        current_account_id: accountId,
        checked_out_at: state?.checked_out_at,
        checkout_request_id: checkoutRequestId,
        checkout_started_at: state?.checkout_started_at,
        pending_checkin_account_id: accountId,
        pending_checkin_checkout_request_id: checkoutRequestId,
        checkin_started_at: new Date().toISOString()
    });
}

function getCheckinRetryDelay(attempt: number): number {
    return CHECKIN_RETRY_DELAYS_MS[Math.min(attempt - 1, CHECKIN_RETRY_DELAYS_MS.length - 1)];
}

function getCheckoutRetryDelay(attempt: number): number {
    return CHECKOUT_RETRY_DELAYS_MS[Math.min(attempt - 1, CHECKOUT_RETRY_DELAYS_MS.length - 1)];
}

function sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function manualReturn() {
    if (!currentAccountId) {
        vscode.window.showInformationMessage("当前无活跃账号。");
        return;
    }

    const auth = readAuthJson();
    if (!auth) {
        vscode.window.showErrorMessage("本地 auth.json 丢失，无法安全归还账号。");
        return;
    }

    try {
        markCheckinPending(currentAccountId);
        stopHeartbeatTimer();
        await checkinCurrentAccountWithRetry(currentAccountId);
    } catch (e: any) {
        vscode.window.showErrorMessage(`归还账号失败：${e.message || e}`);
        return;
    }
    
    clearClientState();
    await performCheckout();
}

export function deactivate() {
    if (heartbeatTimer) {
        clearTimeout(heartbeatTimer);
    }
    if (statusBarItem) {
        statusBarItem.dispose();
    }
    // VSCode 关闭：停心跳，不 checkin，账号保持 IN_USE
}

function updateStatusBar(info: any) {
    if (info.status) {
        statusBarItem.text = `$(account) Codex Pool: ${info.status}`;
    } else {
        const fiveHour = typeof info.five_hour_percent_left === 'number'
            ? `5h ${Math.round(info.five_hour_percent_left)}%`
            : `${info.remaining_pct}/${info.limit_pct}%`;
        const weekly = typeof info.weekly_percent_left === 'number'
            ? ` | 7d ${Math.round(info.weekly_percent_left)}%`
            : '';
        statusBarItem.text = `$(account) ${info.account_id} | ${fiveHour}${weekly}`;
        statusBarItem.tooltip = [
            info.five_hour_reset_at ? `5h reset: ${info.five_hour_reset_at}` : undefined,
            info.weekly_reset_at ? `7d reset: ${info.weekly_reset_at}` : undefined,
            info.reset_requests ? `raw reset: ${info.reset_requests}` : undefined
        ].filter(Boolean).join('\n');
    }
}
