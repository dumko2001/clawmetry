#!/usr/bin/env bash
set -e

CLAWMETRY_APP="https://app.clawmetry.com"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}  🦞 ClawMetry${NC}"
echo -e "  Real-time observability for OpenClaw agents"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo -e "${RED}  ✗ Python 3 not found. Install it from https://python.org${NC}"
  exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "  ${GREEN}✓${NC} Python $PYTHON_VERSION found"

# Check pip
if ! python3 -m pip --version &>/dev/null; then
  echo -e "${RED}  ✗ pip not found. Run: python3 -m ensurepip${NC}"
  exit 1
fi

# Install / upgrade clawmetry
# Try in order: system → user → break-system-packages (PEP 668 envs)
echo -e "  ${CYAN}→${NC} Installing ClawMetry..."
if python3 -m pip install --upgrade clawmetry 2>/dev/null; then
  :
elif python3 -m pip install --upgrade --user clawmetry 2>/dev/null; then
  :
elif python3 -m pip install --upgrade --break-system-packages clawmetry 2>/dev/null; then
  :
else
  echo -e "${RED}  ✗ Failed to install clawmetry. Try: pip install clawmetry${NC}"
  exit 1
fi

CLAWMETRY_VERSION=$(python3 -c "import importlib.metadata; print(importlib.metadata.version('clawmetry'))" 2>/dev/null || echo "?")
echo -e "  ${GREEN}✓${NC} ClawMetry $CLAWMETRY_VERSION installed"
echo ""

# Run onboarding wizard
clawmetry onboard

echo ""
echo -e "  ${GREEN}${BOLD}Done!${NC} Open ${CYAN}${CLAWMETRY_APP}${NC} to see your agents."
echo ""
