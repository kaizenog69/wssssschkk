#!/bin/bash
echo "Starting WhatsApp Checker..."

# Kill any existing processes
pkill -f "node server.mjs" 2>/dev/null
pkill -f "python bot.py" 2>/dev/null
sleep 1

# Start WhatsApp API server
echo "Starting WhatsApp API server..."
node server.mjs &
NODE_PID=$!
echo "WhatsApp API PID: $NODE_PID"

# Wait for server to start
sleep 5

echo "Starting Telegram bot..."
python3 bot.py &
PYTHON_PID=$!
echo "Bot PID: $PYTHON_PID"

echo ""
echo "Services started!"
echo "  WhatsApp API: http://localhost:3001"
echo "  QR endpoint:  http://localhost:3001/qr"
echo ""

wait
