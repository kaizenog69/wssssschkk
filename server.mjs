import express from 'express';
import bodyParser from 'body-parser';
import cors from 'cors';
import SessionManager from './whatsapp.mjs';

const app = express();
const port = process.env.WA_API_PORT || 3000;

app.use(cors());
app.use(bodyParser.json({ limit: '10mb' }));

const sessionManager = new SessionManager();
console.log('Starting WhatsApp Baileys...');
sessionManager.restoreExistingSessions();

app.post('/check', async (req, res) => {
    const { number, userId } = req.body;
    if (!number) return res.status(400).json({ error: 'Number required' });
    if (!userId) return res.status(400).json({ error: 'userId required' });

    const session = sessionManager.getSession(userId);
    if (!session || !session.connected) {
        return res.json({ success: false, error: 'WhatsApp not connected. Use /pair to connect first.', number });
    }

    console.log(`[${userId}] Checking: ${number}`);
    const result = await session.checkNumber(number);
    res.json(result);
});

app.post('/check-bulk', async (req, res) => {
    const { numbers, userId } = req.body;

    if (!numbers || !Array.isArray(numbers)) {
        return res.status(400).json({ error: 'Numbers array required' });
    }
    if (!userId) return res.status(400).json({ error: 'userId required' });

    const session = sessionManager.getSession(userId);
    if (!session || !session.connected) {
        return res.json({
            success: false,
            error: 'WhatsApp not connected. Use /pair to connect first.',
            results: numbers.map(n => ({ success: false, number: n, error: 'Not connected' }))
        });
    }

    console.log(`[${userId}] Bulk checking ${numbers.length} numbers`);
    const startTime = Date.now();

    const results = await session.checkMultiple(numbers);

    const duration = ((Date.now() - startTime) / 1000).toFixed(1);
    console.log(`[${userId}] Done in ${duration}s`);

    res.json({
        success: true,
        results,
        total: numbers.length,
        duration: `${duration}s`
    });
});

app.get('/status', (req, res) => {
    const userId = req.query.userId;
    if (userId) {
        const session = sessionManager.getSession(userId);
        if (session) {
            return res.json(session.getStatus());
        }
        return res.json({ connected: false, userId, message: 'No session found. Use /pair to connect.' });
    }
    res.json({ sessions: sessionManager.getAllStatus() });
});

app.post('/pair', async (req, res) => {
    const { number, userId } = req.body;
    if (!number) return res.status(400).json({ error: 'Phone number required' });
    if (!userId) return res.status(400).json({ error: 'userId required' });

    const session = await sessionManager.getOrCreateSession(userId);

    await new Promise(resolve => setTimeout(resolve, 2000));

    const result = await session.requestPairingCode(number);
    res.json(result);
});

app.post('/disconnect', async (req, res) => {
    const { userId } = req.body;
    if (!userId) return res.status(400).json({ error: 'userId required' });

    await sessionManager.removeSession(userId);
    res.json({ success: true, message: 'Session disconnected and removed.' });
});

app.get('/qr', (req, res) => {
    const userId = req.query.userId;
    if (!userId) return res.status(400).json({ error: 'userId required' });

    const session = sessionManager.getSession(userId);
    if (!session) {
        return res.json({ connected: false, message: 'No session. Use /pair first.' });
    }

    const status = session.getStatus();
    if (status.qrBase64) {
        res.json({ qr: session.qr, qrBase64: status.qrBase64, connected: false });
    } else if (status.connected) {
        res.json({ connected: true, message: 'Already connected' });
    } else {
        res.json({ connected: false, message: 'Waiting for QR...' });
    }
});

app.get('/', (req, res) => {
    res.json({
        service: 'WhatsApp Checker API (Multi-Session)',
        status: 'running',
        activeSessions: sessionManager.sessions.size,
        features: ['Per-user sessions', 'Unlimited checks', 'Parallel processing']
    });
});

app.listen(port, () => {
    console.log(`\nWhatsApp API running on port ${port}`);
    console.log(`Status: http://localhost:${port}/status`);
});

process.on('SIGINT', () => {
    console.log('Shutting down...');
    process.exit(0);
});
