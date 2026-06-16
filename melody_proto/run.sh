#!/usr/bin/env bash
# Run the SATB chorale generator
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/venv12/bin/python3" "$SCRIPT_DIR/generate.py" "$@"
