#!/bin/bash
set -e

cd /home/runner/workspace/wa-checker

echo "======================================="
echo "  WhatsApp Number Checker Bot"
echo "======================================="

# Kill existing processes
pkill -f "node server.mjs" 2>/dev/null || true
pkill -f "python3 bot.py" 2>/dev/null || true
sleep 1

echo "[1/2] Starting WhatsApp API server (port 3001)..."
node server.mjs &
NODE_PID=$!

echo "      Waiting for WhatsApp server..."
sleep 6

if [ "$REPLIT_DEPLOYMENT" = "1" ]; then
    echo "[2/2] Starting Telegram Bot..."
    python3 bot.py &
    BOT_PID=$!
else
    echo "[2/2] Telegram Bot skipped (dev mode - production bot handles it)"
    BOT_PID=""
fi

echo ""
echo "All services running!"
echo "  WhatsApp API: http://localhost:3001"
echo "  QR Code:      http://localhost:3001/qr"
echo ""
echo "Scan QR code to link WhatsApp."
echo "======================================="

# Wait for processes
if [ -n "$BOT_PID" ]; then
    wait $NODE_PID $BOT_PID
else
    wait $NODE_PID
fi
