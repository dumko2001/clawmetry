#!/usr/bin/env bash
# Run ClawMetry landing tests locally before deploying.
# Usage:
#   ./run_tests.sh              # unit + content tests only (fast, no network)
#   ./run_tests.sh --full       # + integration tests (hits Resend + live site)
#   ./run_tests.sh --deploy     # run all tests then deploy if passing

set -e
cd "$(dirname "$0")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

MODE="unit"
[[ "$1" == "--full" ]] && MODE="full"
[[ "$1" == "--deploy" ]] && MODE="deploy"

echo -e "${YELLOW}━━━ ClawMetry Landing Tests ━━━${NC}"

# Ensure deps
if ! python3 -c "import pytest" 2>/dev/null; then
  echo "Installing test deps..."
  pip install pytest pytest-cov requests -q
fi

# Unit + content tests (always run)
echo -e "\n${YELLOW}▶ Unit & Content Tests${NC}"
TESTING=1 SKIP_INTEGRATION=1 \
  python3 -m pytest tests/test_pages.py tests/test_api.py \
    -v --tb=short --no-header -q
echo -e "${GREEN}✓ Unit tests passed${NC}"

# Integration tests (optional)
if [[ "$MODE" == "full" || "$MODE" == "deploy" ]]; then
  echo -e "\n${YELLOW}▶ Integration Tests (Resend + live site)${NC}"
  SKIP_INTEGRATION=0 \
    python3 -m pytest tests/test_integration.py \
      -v --tb=short --no-header
  echo -e "${GREEN}✓ Integration tests passed${NC}"
fi

# Deploy (only if --deploy flag and all tests green)
if [[ "$MODE" == "deploy" ]]; then
  echo -e "\n${YELLOW}▶ Deploying to Cloud Run...${NC}"
  cd ..
  PROJECT_ID=openclaw-mc bash scripts/deploy-landing.sh
  echo -e "${GREEN}✓ Deployed!${NC}"
fi

echo -e "\n${GREEN}━━━ All checks passed ━━━${NC}"
