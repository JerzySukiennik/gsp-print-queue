#!/bin/bash
# GSP Print Queue: create venv on first run, then start the server.
cd "$(dirname "$0")"
if [ ! -x venv/bin/python ]; then
  python3 -m venv venv && venv/bin/pip install -q -r requirements.txt
fi
exec venv/bin/python server.py
