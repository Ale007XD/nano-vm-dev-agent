#!/usr/bin/env bash
# setup.sh — verify environment before running nano-vm-dev-agent
set -euo pipefail

echo "==> Checking Python version..."
python3 -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'"
echo "    OK: $(python3 --version)"

echo "==> Checking llm-nano-vm..."
python3 -c "import nano_vm; print('    OK: llm-nano-vm installed')"

echo "==> Checking mypy..."
mypy --version > /dev/null
echo "    OK: $(mypy --version)"

echo "==> Checking pytest..."
pytest --version > /dev/null
echo "    OK: $(pytest --version)"

echo "==> Checking ANTHROPIC_API_KEY..."
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "    ERROR: ANTHROPIC_API_KEY is not set"
    echo "    Copy .env.example to .env and fill in your key"
    exit 1
fi
echo "    OK: key is set"

echo ""
echo "Environment OK. Run the agent with:"
echo "  python -c \"import asyncio; from agent.runner import run_sprint; ...\""
