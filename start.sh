#!/usr/bin/env sh
set -eu
python -m pip install -r requirements.txt
chmod +x tools/astcenc
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
