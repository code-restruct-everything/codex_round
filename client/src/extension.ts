import * as vscode from 'vscode';
import { readClientState, writeClientState, readAuthJson, writeAuthJson, clearClientState } from './utils';
import { checkoutAccount, checkinAccount, deleteAccount, updateAccountUsage } from './vaultApi';
import { runClientHeartbeat, QuotaInfo } from './heartbeat';

let heartbeatTimer: NodeJS.Timeout | undefined;
let statusBarItem: vscode.StatusBarItem;
let currentAccountId: string | undefined;

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
    if (state?.current_account_id) {
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
    const result = await checkoutAccount();
    if (result) {
        currentAccountId = result.account_id;
        writeAuthJson(result.auth_json);
        writeClientState({
            current_account_id: currentAccountId,
            checked_out_at: new Date().toISOString()
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
    } else {
        currentAccountId = undefined;
        updateStatusBar({ status: '无可用账号' });
    }
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
        await deleteAccount(currentAccountId);
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
            vscode.window.showInformationMessage(`账号 ${currentAccountId} 额度即将耗尽，正在切换...`);
            try {
                const latestAuth = readAuthJson();
                if (latestAuth) {
                    await checkinAccount(currentAccountId, latestAuth);
                }
            } catch (e: any) {
                console.error("Checkin failed during auto switch", e.message);
            }
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

async function manualReturn() {
    if (!currentAccountId) {
        vscode.window.showInformationMessage("当前无活跃账号。");
        return;
    }

    const auth = readAuthJson();
    if (auth) {
        try {
            await checkinAccount(currentAccountId, auth);
            vscode.window.showInformationMessage(`成功归还账号: ${currentAccountId}`);
        } catch (e) {
            vscode.window.showErrorMessage("归还账号失败。");
            return;
        }
    }
    
    clearClientState();
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
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
