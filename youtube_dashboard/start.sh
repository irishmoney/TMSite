#!/bin/bash
set -e
cd "$(dirname "$0")"

# Point to your credentials file (override with env var if needed)
export YT_CLIENT_SECRET="${YT_CLIENT_SECRET:-$HOME/Desktop/client_secret.json}"

if [ ! -f "$YT_CLIENT_SECRET" ]; then
  echo "ERROR: credentials file not found at $YT_CLIENT_SECRET"
  echo "Set YT_CLIENT_SECRET env var to your client_secret.json path."
  exit 1
fi

# Create venv if needed
if [ ! -d "venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv venv
fi

source venv/bin/activate

# Install/upgrade deps
pip install -q -r requirements.txt

echo ""
echo "======================================"
echo "  YouTube Analytics Dashboard"
echo "  Open: http://localhost:8080"
echo "======================================"
echo ""

python app.py
