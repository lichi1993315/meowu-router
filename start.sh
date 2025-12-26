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

# Start router.py
echo "Starting router.py..."
nohup python -u router.py > output.log 2>&1 &
echo "router.py started with PID $!"
