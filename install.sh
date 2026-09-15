#!/usr/bin/env bash
# =============================================================================
# install.sh — One-time setup for the Dixell Docker stack on the Pi.
#
# Run this from inside the folder where you extracted the bundle.
# It detects the Dixell repo (the folder containing collector_shaprepoint.py),
# installs Docker if needed, and copies the Docker files into that repo.
#
# Because docker-compose.yml uses a RELATIVE bind mount ( .:/app ), there is
# no absolute path to configure. You just run compose from inside the repo.
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

info "=== Dixell Docker install ==="

# ── 1. Find the Dixell repo (folder with collector_shaprepoint.py) ───────────
# Default assumption: the bundle was extracted INTO the repo, or the repo is
# the current directory. Override by passing the repo path as the first arg:
#   ./install.sh /home/gyre/dixell_dockerized-main
REPO_DIR="${1:-$SCRIPT_DIR}"

if [ ! -f "$REPO_DIR/collector_shaprepoint.py" ] && [ ! -f "$REPO_DIR/emitter.py" ]; then
    error "Could not find Dixell scripts in: $REPO_DIR"
    error "Pass the repo path explicitly:  ./install.sh /path/to/dixell_dockerized-main"
    exit 1
fi
info "Dixell repo: $REPO_DIR"

# ── 2. Serial device check ──────────────────────────────────────────────────
if [ -e /dev/ttyACM0 ]; then
    info "Serial device: $(ls -la /dev/ttyACM0)"
else
    warn "/dev/ttyACM0 not present. That's OK — the collector container will"
    warn "wait for it and start automatically once the controller is plugged in."
fi

# ── 3. Install Docker if missing ────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
else
    info "Docker present: $(docker --version)"
fi

# ── 4. Docker group ─────────────────────────────────────────────────────────
CURRENT_USER="$(whoami)"
if groups "$CURRENT_USER" | grep -qw docker; then
    info "User $CURRENT_USER already in docker group."
else
    info "Adding $CURRENT_USER to docker group..."
    sudo usermod -aG docker "$CURRENT_USER"
    warn "LOG OUT AND BACK IN for this to take effect. Until then, use 'sudo docker ...'"
fi

# ── 5. Compose plugin ───────────────────────────────────────────────────────
if ! docker compose version &>/dev/null && ! sudo docker compose version &>/dev/null; then
    info "Installing docker-compose-plugin..."
    sudo apt-get update && sudo apt-get install -y docker-compose-plugin
else
    info "Docker Compose present."
fi

# ── 6. Copy Docker files into the repo ──────────────────────────────────────
info "Copying Docker files into $REPO_DIR ..."
for f in Dockerfile .dockerignore docker-compose.yml supervisor.py wait_and_run.py requirements.txt dixell_modbus.py xr77u.json; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        error "Missing from bundle: $f"; exit 1
    fi
    # Don't overwrite-copy onto itself if installing in place
    if [ "$SCRIPT_DIR/$f" != "$REPO_DIR/$f" ]; then
        cp -v "$SCRIPT_DIR/$f" "$REPO_DIR/"
    fi
done

echo ""
info "=== Install complete ==="
echo ""
echo "Next steps:"
echo ""
echo "  cd $REPO_DIR"
echo "  docker compose build"
echo "  docker compose up -d collector emitter"
echo "  docker compose logs -f collector"
echo "  docker compose logs -f emitter"
echo ""
echo "(prefix with sudo until you have logged out/in for the docker group)"
