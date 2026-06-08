"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.readAuthJson = readAuthJson;
exports.writeAuthJson = writeAuthJson;
exports.readClientState = readClientState;
exports.writeClientState = writeClientState;
exports.clearClientState = clearClientState;
const fs = require("fs");
const path = require("path");
const os = require("os");
const CODEX_DIR = path.join(os.homedir(), '.codex');
const AUTH_JSON_PATH = path.join(CODEX_DIR, 'auth.json');
const STATE_FILE_PATH = path.join(os.homedir(), '.codex-client-state.json');
function readAuthJson() {
    try {
        if (!fs.existsSync(AUTH_JSON_PATH)) {
            return null;
        }
        const data = fs.readFileSync(AUTH_JSON_PATH, 'utf-8');
        return JSON.parse(data);
    }
    catch (e) {
        console.error('Failed to read auth.json', e);
        return null;
    }
}
function writeAuthJson(auth) {
    try {
        if (!fs.existsSync(CODEX_DIR)) {
            fs.mkdirSync(CODEX_DIR, { recursive: true });
        }
        fs.writeFileSync(AUTH_JSON_PATH, JSON.stringify(auth, null, 2), 'utf-8');
    }
    catch (e) {
        console.error('Failed to write auth.json', e);
    }
}
function readClientState() {
    try {
        if (!fs.existsSync(STATE_FILE_PATH)) {
            return null;
        }
        const data = fs.readFileSync(STATE_FILE_PATH, 'utf-8');
        return JSON.parse(data);
    }
    catch (e) {
        console.error('Failed to read client state', e);
        return null;
    }
}
function writeClientState(state) {
    try {
        fs.writeFileSync(STATE_FILE_PATH, JSON.stringify(state, null, 2), 'utf-8');
    }
    catch (e) {
        console.error('Failed to write client state', e);
    }
}
function clearClientState() {
    try {
        if (fs.existsSync(STATE_FILE_PATH)) {
            fs.unlinkSync(STATE_FILE_PATH);
        }
    }
    catch (e) {
        console.error('Failed to clear client state', e);
    }
}
//# sourceMappingURL=utils.js.map