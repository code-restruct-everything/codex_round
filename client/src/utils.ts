import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

export interface AuthJson {
    accessToken?: string;
    refreshToken?: string;
    access_token?: string;
    refresh_token?: string;
    tokens?: {
        access_token?: string;
        accessToken?: string;
        refresh_token?: string;
        refreshToken?: string;
        account_id?: string;
        accountId?: string;
        [key: string]: any;
    };
    expiresAt?: number;
    expires_in?: number;
    userId?: string;
    account_id?: string;
    accountId?: string;
    chatgpt_account_id?: string;
    chatgptAccountId?: string;
    [key: string]: any;
}

export interface ClientState {
    current_account_id?: string;
    checked_out_at?: string;
    checkout_request_id?: string;
    checkout_started_at?: string;
    pending_checkout_account_id?: string;
    pending_checkin_account_id?: string;
    pending_checkin_checkout_request_id?: string;
    checkin_started_at?: string;
}

const CODEX_DIR = path.join(os.homedir(), '.codex');
const AUTH_JSON_PATH = path.join(CODEX_DIR, 'auth.json');
const STATE_FILE_PATH = path.join(os.homedir(), '.codex-client-state.json');

function atomicWriteFile(filePath: string, content: string): void {
    const tmpPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
    let fd: number | undefined;
    try {
        fd = fs.openSync(tmpPath, 'w');
        fs.writeFileSync(fd, content, 'utf-8');
        fs.fsyncSync(fd);
        fs.closeSync(fd);
        fd = undefined;
        fs.renameSync(tmpPath, filePath);
    } finally {
        if (fd !== undefined) {
            fs.closeSync(fd);
        }
        if (fs.existsSync(tmpPath)) {
            fs.unlinkSync(tmpPath);
        }
    }
}

export function readAuthJson(): AuthJson | null {
    try {
        if (!fs.existsSync(AUTH_JSON_PATH)) {
            return null;
        }
        const data = fs.readFileSync(AUTH_JSON_PATH, 'utf-8');
        return JSON.parse(data);
    } catch (e) {
        console.error('Failed to read auth.json', e);
        return null;
    }
}

export function writeAuthJson(auth: AuthJson): void {
    try {
        if (!fs.existsSync(CODEX_DIR)) {
            fs.mkdirSync(CODEX_DIR, { recursive: true });
        }
        atomicWriteFile(AUTH_JSON_PATH, JSON.stringify(auth, null, 2));
    } catch (e) {
        console.error('Failed to write auth.json', e);
        throw e;
    }
}

export function readClientState(): ClientState | null {
    try {
        if (!fs.existsSync(STATE_FILE_PATH)) {
            return null;
        }
        const data = fs.readFileSync(STATE_FILE_PATH, 'utf-8');
        return JSON.parse(data);
    } catch (e) {
        console.error('Failed to read client state', e);
        return null;
    }
}

export function writeClientState(state: ClientState): void {
    try {
        atomicWriteFile(STATE_FILE_PATH, JSON.stringify(state, null, 2));
    } catch (e) {
        console.error('Failed to write client state', e);
        throw e;
    }
}

export function clearClientState(): void {
    try {
        if (fs.existsSync(STATE_FILE_PATH)) {
            fs.unlinkSync(STATE_FILE_PATH);
        }
    } catch (e) {
        console.error('Failed to clear client state', e);
        throw e;
    }
}
