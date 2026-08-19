#!/usr/bin/env bash
# LansCoder one-line installer
# Supports macOS, Linux, and Windows (Git Bash / WSL2)
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Lanstzz/LansCoder/main/install.sh | bash

set -euo pipefail

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── Banner ──
echo ""
echo -e "${BLUE}╔══════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}        ${BOLD}LansCoder Installer${NC}           ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   A coding agent you can read       ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════╝${NC}"
echo ""

# ── Platform detection ──
OS="$(uname -s)"
case "$OS" in
  Darwin)  PLATFORM="macOS" ;;
  Linux)   PLATFORM="Linux" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="Windows" ;;
  *)       PLATFORM="$OS" ;;
esac
echo -e "${CYAN}→${NC} Detected platform: ${BOLD}$PLATFORM${NC}"

# ── Step 1: Check Python ──
echo -e "${CYAN}→${NC} Checking Python 3.11+ ..."

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" &>/dev/null; then
    version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0")
    major=$(echo "$version" | cut -d. -f1)
    minor=$(echo "$version" | cut -d. -f2)
    if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
      PYTHON="$candidate"
      echo -e "  ${GREEN}✓${NC} Found $candidate (Python $version)"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo -e "  ${RED}✗${NC} Python 3.11+ is required but not found."
  echo ""
  echo -e "  ${YELLOW}Install Python first:${NC}"
  case "$PLATFORM" in
    macOS)
      echo "    brew install python@3.12"
      echo "    or download from https://www.python.org/downloads/"
      ;;
    Linux)
      echo "    sudo apt install python3.12  (Debian/Ubuntu)"
      echo "    sudo dnf install python3.12  (Fedora)"
      ;;
    Windows)
      echo "    winget install Python.Python.3.12"
      echo "    or download from https://www.python.org/downloads/"
      ;;
  esac
  exit 1
fi

# ── Step 2: Check / install pipx ──
echo -e "${CYAN}→${NC} Checking pipx ..."

if command -v pipx &>/dev/null; then
  echo -e "  ${GREEN}✓${NC} pipx found"
else
  echo -e "  ${YELLOW}!${NC} pipx not found, installing ..."
  case "$PLATFORM" in
    macOS)
      if command -v brew &>/dev/null; then
        brew install pipx
      else
        "$PYTHON" -m pip install --user pipx
      fi
      ;;
    Linux)
      if command -v apt &>/dev/null; then
        sudo apt update && sudo apt install -y pipx
      elif command -v dnf &>/dev/null; then
        sudo dnf install -y pipx
      else
        "$PYTHON" -m pip install --user pipx
      fi
      ;;
    Windows)
      "$PYTHON" -m pip install --user pipx
      ;;
  esac

  # Ensure pipx is on PATH
  "$PYTHON" -m pipx ensurepath 2>/dev/null || true
  export PATH="$HOME/.local/bin:$PATH"

  if command -v pipx &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} pipx installed successfully"
  else
    echo -e "  ${RED}✗${NC} Failed to install pipx. Please install it manually:"
    echo "    https://pipx.pypa.io/stable/installation/"
    exit 1
  fi
fi

# ── Step 3: Install LansCoder ──
echo -e "${CYAN}→${NC} Installing LansCoder ..."

# Re-fetch PATH in case pipx was just installed
export PATH="$HOME/.local/bin:$PATH"

if pipx list 2>/dev/null | grep -q lanscoder; then
  echo -e "  ${YELLOW}!${NC} LansCoder already installed, upgrading ..."
  pipx upgrade lanscoder 2>/dev/null || pipx install --force lanscoder
else
  pipx install lanscoder
fi

echo -e "  ${GREEN}✓${NC} LansCoder installed"

# ── Step 4: Initialize config ──
echo -e "${CYAN}→${NC} Initializing configuration ..."

if [ -f "$HOME/.config/lanscoder/config.toml" ]; then
  echo -e "  ${YELLOW}!${NC} Config already exists at ~/.config/lanscoder/config.toml"
else
  lanscoder config init 2>/dev/null && \
    echo -e "  ${GREEN}✓${NC} Config created at ~/.config/lanscoder/config.toml" || \
    echo -e "  ${YELLOW}!${NC} Run 'lanscoder config init' manually to create config"
fi

# ── Done ──
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}     ${BOLD}Installation complete!${NC}          ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo -e "  1. Edit your config:  ${CYAN}~/.config/lanscoder/config.toml${NC}"
echo -e "     Add your API key in the ${CYAN}[providers]${NC} section."
echo ""
echo -e "  2. Launch from any project directory:"
echo -e "     ${BOLD}\$ lanscoder${NC}"
echo ""
echo -e "  ${BOLD}Need help?${NC} https://github.com/Lanstzz/LansCoder"
echo ""