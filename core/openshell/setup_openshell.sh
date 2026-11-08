#!/usr/bin/env bash
# One-time setup for OpenShell secured mode.
#
# Installs OpenShell CLI if missing, then runs the Python setup script
# that verifies Docker, bootstraps the gateway, and tests sandbox creation.
#
# Usage:
#     ./connect sandbox-setup

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Install OpenShell CLI if not found
if ! command -v openshell &> /dev/null; then
    echo "Installing OpenShell CLI..."
    curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
    echo ""
fi

# Run Python setup
cd "$SCRIPT_DIR/../.." && python3 -m core.setup_sandbox
