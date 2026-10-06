#!/usr/bin/env bash
set -euo pipefail
uv run --with boto3 python scripts/inspect_connections_500.py
