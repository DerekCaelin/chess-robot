#!/bin/bash
# deploy.sh - push code to the Pi. Run from the PC repo root:
#   bash pi/deploy.sh [user@host]      (default: pi@chesspi)
# Device-local state (data/, images/, points.txt, saved games) is never
# overwritten by code deploys.
HOST="${1:-pi@chesspi}"
SRC="$(cd "$(dirname "$0")/.." && pwd)/"
ssh "$HOST" 'mkdir -p ~/chess' 2>/dev/null || true
rsync -avz --delete \
    --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'data/' --exclude 'images/' --exclude '.git' \
    "$SRC" "$HOST:~/chess/chess-robot-v3/"
echo "deployed to $HOST - restart the UI with:  ssh $HOST '~/chess/chess-robot-v3/pi/appctl restart'"
