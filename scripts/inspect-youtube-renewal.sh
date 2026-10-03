#!/usr/bin/env bash
set -euo pipefail
uv run --with boto3 python scripts/inspect_youtube_renewal.py
