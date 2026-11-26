#!/bin/bash
# Double-click to start TimeEntry on a Mac. Add "demo" as an argument for the demo company.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and run this again."; read -r; exit 1
fi
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
(sleep 3; open "http://127.0.0.1:8000") &
python -m timeentry ${1:+--$1}
