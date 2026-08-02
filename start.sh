#!/bin/bash
# Start the Kin App
cd "$(dirname "$0")"

if [ ! -f ~/.config/kin_app/config.json ]; then
    echo "First run — setting up..."
    python3 setup.py
fi

echo "Starting Echo Bloom on http://localhost:8090"
exec uvicorn main:app --host 0.0.0.0 --port 8090
