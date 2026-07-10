#!/bin/bash
# Abe LeadOps — one-click launcher for macOS.
# Double-click this file. It sets up everything on first run, starts the
# app, and opens it in your browser. Close the Terminal window (or press
# Control-C) to stop it.

cd "$(dirname "$0")" || exit 1
echo "Abe LeadOps — starting up..."
echo "Folder: $(pwd)"
echo

# Pick python3 (fall back to python).
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python is not installed. Install it from https://www.python.org/downloads/ and try again."
  echo "Press Return to close."; read -r _; exit 1
fi

# Create the virtual environment once.
if [ ! -d .venv ]; then
  echo "First-time setup: creating virtual environment..."
  "$PY" -m venv .venv || { echo "Could not create .venv"; read -r _; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Install dependencies once (sentinel makes later launches fast).
if [ ! -f .venv/.deps_installed ]; then
  echo "Installing dependencies (one time, ~30s)..."
  pip install -q --upgrade pip >/dev/null 2>&1
  if pip install -q -r requirements.txt; then
    touch .venv/.deps_installed
  else
    echo "Dependency install failed. Check your internet connection and try again."
    read -r _; exit 1
  fi
fi

# Open the browser shortly after the server starts.
( sleep 3; open "http://127.0.0.1:5000/login" >/dev/null 2>&1 ) &

echo
echo "Opening http://127.0.0.1:5000/login in your browser..."
echo "Leave this window open while you work. Press Control-C here to stop."
echo
python3 app.py

# If the server exits, keep the window open so any message is readable.
echo
echo "Server stopped. Press Return to close."
read -r _
