#!/bin/bash
set -e  # stop if any error happens

echo "Installing dependencies..."
pip install -r requirements.txt
npm install

echo "Starting server..."
node server.mjs &

sleep 5

echo "Starting bot..."
python3 bot.py &

echo "All services started"

wait
