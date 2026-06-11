"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.checkoutAccount = checkoutAccount;
exports.checkinAccount = checkinAccount;
exports.updateAccountUsage = updateAccountUsage;
exports.deleteAccount = deleteAccount;
const axios_1 = require("axios");
const vscode = require("vscode");
function getVaultClient() {
    const config = vscode.workspace.getConfiguration('codexPool');
    const vaultUrl = config.get('vaultUrl', 'http://127.0.0.1:8000');
    const apiKey = config.get('apiKey', '');
    return axios_1.default.create({
        baseURL: vaultUrl,
        headers: {
            'Authorization': `Bearer ${apiKey}`,
            'Content-Type': 'application/json'
        },
        timeout: 15000
    });
}
async function checkoutAccount() {
    const client = getVaultClient();
    try {
        const response = await client.post('/checkout');
        return response.data;
    }
    catch (e) {
        if (e.response && e.response.status === 503) {
            vscode.window.showWarningMessage(`Codex Pool: ${e.response.data.detail}`);
            return null;
        }
        console.error('Checkout failed:', e.message);
        vscode.window.showErrorMessage(`Codex Pool Checkout Failed: ${e.message}`);
        throw e;
    }
}
async function checkinAccount(accountId, authJson, showError = true) {
    const client = getVaultClient();
    try {
        await client.post(`/checkin/${accountId}`, { auth_json: authJson });
        return true;
    }
    catch (e) {
        console.error('Checkin failed:', e.message);
        if (showError) {
            vscode.window.showErrorMessage(`Codex Pool Checkin Failed: ${e.message}`);
        }
        // We throw so caller can implement exponential backoff
        throw e;
    }
}
async function updateAccountUsage(accountId, usage) {
    const client = getVaultClient();
    try {
        await client.post(`/accounts/${accountId}/usage`, usage);
        return true;
    }
    catch (e) {
        console.error('Usage update failed:', e.message);
        return false;
    }
}
async function deleteAccount(accountId) {
    const client = getVaultClient();
    try {
        await client.delete(`/accounts/${accountId}`);
        return true;
    }
    catch (e) {
        console.error('Delete account failed:', e.message);
        return false;
    }
}
//# sourceMappingURL=vaultApi.js.map