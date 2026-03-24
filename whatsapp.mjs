import { webcrypto } from 'node:crypto';
if (!global.crypto) {
    global.crypto = webcrypto;
}

import * as baileys from '@whiskeysockets/baileys';
const { makeWASocket, DisconnectReason, useMultiFileAuthState, fetchLatestBaileysVersion, Browsers } = baileys;

import P from 'pino';
import QRCode from 'qrcode';
import fs from 'fs';
import path from 'path';

const BASE_AUTH_DIR = new URL('./auth_info_baileys', import.meta.url).pathname;
if (!fs.existsSync(BASE_AUTH_DIR)) {
    fs.mkdirSync(BASE_AUTH_DIR, { recursive: true });
}

class WhatsAppSession {
    constructor(userId) {
        this.userId = userId;
        this.sock = null;
        this.qr = null;
        this.qrBase64 = null;
        this.pairingCode = null;
        this.connected = false;
        this.authFolder = path.join(BASE_AUTH_DIR, String(userId));
        this.initializing = false;
        if (!fs.existsSync(this.authFolder)) {
            fs.mkdirSync(this.authFolder, { recursive: true });
        }
    }

    async initialize() {
        if (this.initializing) return;
        this.initializing = true;
        try {
            const { version, isLatest } = await fetchLatestBaileysVersion();
            console.log(`[${this.userId}] Using WhatsApp Web v${version.join('.')} (Latest: ${isLatest})`);

            const { state, saveCreds } = await useMultiFileAuthState(this.authFolder);

            this.sock = makeWASocket({
                version,
                auth: state,
                logger: P({ level: 'silent' }),
                browser: Browsers.ubuntu('Chrome'),
                syncFullHistory: false,
                markOnlineOnConnect: false,
            });

            this.sock.ev.on('connection.update', async (update) => {
                const { connection, lastDisconnect, qr } = update;

                if (qr) {
                    this.qr = qr;
                    try {
                        this.qrBase64 = await QRCode.toDataURL(qr);
                    } catch (e) {
                        this.qrBase64 = null;
                    }
                    console.log(`[${this.userId}] QR code generated, waiting for scan...`);
                    return;
                }

                if (connection === 'close') {
                    const statusCode = lastDisconnect?.error?.output?.statusCode;
                    const errorMessage = lastDisconnect?.error?.message || 'Unknown error';

                    console.log(`[${this.userId}] Connection closed. Status: ${statusCode || 'N/A'}`);
                    this.connected = false;
                    this.qrBase64 = null;
                    this.initializing = false;

                    if (statusCode === 405 || statusCode === DisconnectReason.badSession ||
                        errorMessage.includes('405') || errorMessage.includes('auth')) {
                        console.log(`[${this.userId}] Auth error. Clearing session...`);
                        this.clearAuthFolder();
                        console.log(`[${this.userId}] Restarting in 3s...`);
                        setTimeout(() => this.initialize(), 3000);
                        return;
                    }

                    if (statusCode === DisconnectReason.loggedOut) {
                        console.log(`[${this.userId}] Logged out.`);
                        this.clearAuthFolder();
                        return;
                    }

                    console.log(`[${this.userId}] Reconnecting in 5s...`);
                    setTimeout(() => this.initialize(), 5000);

                } else if (connection === 'connecting') {
                    console.log(`[${this.userId}] Connecting to WhatsApp...`);

                } else if (connection === 'open') {
                    console.log(`[${this.userId}] WhatsApp connected!`);
                    this.connected = true;
                    this.qr = null;
                    this.qrBase64 = null;
                    this.initializing = false;
                }
            });

            this.sock.ev.on('creds.update', saveCreds);

        } catch (error) {
            console.error(`[${this.userId}] Init error:`, error.message);
            this.initializing = false;
            setTimeout(() => this.initialize(), 10000);
        }
    }

    clearAuthFolder() {
        try {
            if (fs.existsSync(this.authFolder)) {
                fs.rmSync(this.authFolder, { recursive: true, force: true });
                console.log(`[${this.userId}] Auth cleared`);
            }
        } catch (err) {
            console.error(`[${this.userId}] Error clearing auth:`, err.message);
        }
    }

    async checkNumber(phoneNumber, retries = 2) {
        if (!this.connected || !this.sock) {
            return {
                success: false,
                error: 'WhatsApp not connected. Please pair first using /pair command.'
            };
        }

        try {
            const cleanNumber = phoneNumber.replace(/\D/g, '');

            if (cleanNumber.length < 10) {
                return {
                    success: false,
                    error: 'Number too short'
                };
            }

            const whatsappId = `${cleanNumber}@s.whatsapp.net`;
            const [result] = await this.sock.onWhatsApp(whatsappId);

            return {
                success: true,
                exists: result?.exists || false,
                jid: result?.jid || null,
                number: cleanNumber
            };

        } catch (error) {
            if (retries > 0) {
                return this.checkNumber(phoneNumber, retries - 1);
            }
            console.error(`[${this.userId}] Check error:`, error.message);
            return {
                success: false,
                error: error.message
            };
        }
    }

    async checkMultiple(numbers) {
        const results = [];
        for (let i = 0; i < numbers.length; i++) {
            const result = await this.checkNumber(numbers[i]);
            results.push(result);
        }
        return results;
    }

    async requestPairingCode(phoneNumber) {
        if (this.connected) {
            return { success: false, error: 'Already connected' };
        }
        if (!this.sock) {
            return { success: false, error: 'Socket not initialized. Wait a few seconds.' };
        }
        try {
            const cleanNumber = phoneNumber.replace(/\D/g, '');
            const code = await this.sock.requestPairingCode(cleanNumber);
            this.pairingCode = code;
            console.log(`[${this.userId}] Pairing code for ${cleanNumber}: ${code}`);
            return { success: true, code };
        } catch (error) {
            console.error(`[${this.userId}] Pairing code error:`, error.message);
            return { success: false, error: error.message };
        }
    }

    getStatus() {
        return {
            connected: this.connected,
            hasQR: !!this.qr,
            qrBase64: this.qrBase64,
            pairingCode: this.pairingCode,
            userId: this.userId,
            timestamp: new Date().toISOString()
        };
    }

    destroy() {
        try {
            if (this.sock) {
                this.sock.end();
                this.sock = null;
            }
        } catch (e) {}
    }
}

class SessionManager {
    constructor() {
        this.sessions = new Map();
    }

    getSession(userId) {
        return this.sessions.get(String(userId)) || null;
    }

    async getOrCreateSession(userId) {
        const id = String(userId);
        if (this.sessions.has(id)) {
            return this.sessions.get(id);
        }
        const session = new WhatsAppSession(id);
        this.sessions.set(id, session);
        await session.initialize();
        return session;
    }

    async removeSession(userId) {
        const id = String(userId);
        const session = this.sessions.get(id);
        if (session) {
            session.destroy();
            session.clearAuthFolder();
            this.sessions.delete(id);
        }
    }

    restoreExistingSessions() {
        try {
            if (!fs.existsSync(BASE_AUTH_DIR)) return;
            const dirs = fs.readdirSync(BASE_AUTH_DIR);
            for (const dir of dirs) {
                const fullPath = path.join(BASE_AUTH_DIR, dir);
                if (fs.statSync(fullPath).isDirectory()) {
                    const credsPath = path.join(fullPath, 'creds.json');
                    if (fs.existsSync(credsPath)) {
                        console.log(`Restoring session for user: ${dir}`);
                        const session = new WhatsAppSession(dir);
                        this.sessions.set(dir, session);
                        session.initialize();
                    }
                }
            }
        } catch (err) {
            console.error('Error restoring sessions:', err.message);
        }
    }

    getAllStatus() {
        const statuses = {};
        for (const [id, session] of this.sessions) {
            statuses[id] = session.getStatus();
        }
        return statuses;
    }
}

export default SessionManager;
