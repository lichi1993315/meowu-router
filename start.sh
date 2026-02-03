#!/bin/bash

PORT=9000

# Check/Kill process on port 9000
PID=$(lsof -t -i:$PORT)
if [ -n "$PID" ]; then
    echo "Port $PORT is occupied by PID $PID. Killing it..."
    kill -9 $PID
else
    echo "Port $PORT is free."
fi

# Start API (legacy or new)
export DB_PATH="$(pwd)/data/conversations.db"
if [ "${USE_NEW_APP}" = "1" ]; then
    echo "Starting app.main:app..."
    nohup uvicorn app.main:app --host 0.0.0.0 --port $PORT > output.log 2>&1 &
    echo "app.main:app started with PID $!"
else
    echo "Starting router.py..."
    nohup python -u router.py > output.log 2>&1 &
    echo "router.py started with PID $!"
fi
