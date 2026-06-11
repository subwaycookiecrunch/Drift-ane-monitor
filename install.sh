#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
echo "drift installed. Run with ./run.sh"
