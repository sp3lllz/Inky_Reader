#!/usr/bin/env bash
# ============================================================================
# Inky Impression 4" E-Reader — Automated Pi Setup
# ============================================================================
#
# Run this on a fresh Raspberry Pi OS install:
#
#   curl -sL https://raw.githubusercontent.com/sp3lllz/Inky_Reader/main/setup.sh | bash
#
# Or download and run manually:
#
#   wget -O setup.sh https://raw.githubusercontent.com/sp3lllz/Inky_Reader/main/setup.sh
#   chmod +x setup.sh
#   bash setup.sh
#
# What this script does:
#   1. Updates the system
#   2. Enables SPI and I2C interfaces
#   3. Installs system dependencies and fonts
#   4. Installs Python packages (inky, Pillow, RPi.GPIO)
#   5. Downloads ereader.py and epub2txt.py
#   6. Creates ~/books/ with a sample book from Project Gutenberg
#   7. Sets up a systemd service for auto-start on boot
#   8. Configures GPIO wake from suspend (optional)
#   9. Adds passwordless sudo for suspend
#  10. Reboots to apply SPI/I2C changes
#
# ============================================================================

set -e

# ---------------------------------------------------------------------------
# Configuration — edit these if you've forked the repo
# ---------------------------------------------------------------------------

# Where to download the scripts from (raw GitHub URLs)
REPO_BASE="https://raw.githubusercontent.com/sp3lllz/Inky_Reader/main"
EREADER_URL="${REPO_BASE}/ereader.py"
EPUB2TXT_URL="${REPO_BASE}/epub2txt.py"

# Local install paths
INSTALL_DIR="${HOME}/ereader"
BOOKS_DIR="${HOME}/books"

# Which GPIO pin wakes from sleep (Button A = GPIO 5)
WAKE_GPIO=5

# The user running the e-reader (auto-detected)
EREADER_USER="$(whoami)"

# ---------------------------------------------------------------------------
# Colours and helpers
# ---------------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No colour

step_num=0

step() {
    step_num=$((step_num + 1))
    echo ""
    echo -e "${BLUE}${BOLD}[${step_num}] $1${NC}"
    echo -e "${BLUE}$(printf '%.0s─' {1..60})${NC}"
}

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
info() { echo -e "  ${CYAN}→${NC} $1"; }

confirm() {
    echo ""
    read -r -p "$(echo -e "${BOLD}$1 [Y/n]${NC} ")" response
    case "$response" in
        [nN][oO]|[nN]) return 1 ;;
        *) return 0 ;;
    esac
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║      Inky Impression 4\" E-Reader — Pi Setup            ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Must be running on a Pi (or at least Linux ARM)
if [[ ! -f /proc/device-tree/model ]]; then
    warn "Can't detect Pi model — this script is designed for Raspberry Pi."
    if ! confirm "Continue anyway?"; then
        echo "Aborted."
        exit 1
    fi
else
    PI_MODEL=$(tr -d '\0' < /proc/device-tree/model)
    ok "Detected: ${PI_MODEL}"
fi

# Check we're not root (we'll use sudo where needed)
if [[ "$EUID" -eq 0 ]]; then
    fail "Don't run this script as root. Run as your normal user (e.g. pi)."
    echo "  The script will use sudo where needed."
    exit 1
fi

# Check sudo access
if ! sudo -n true 2>/dev/null; then
    info "Sudo password may be required during setup."
fi

OS_VERSION=$(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'"' -f2)
ok "OS: ${OS_VERSION:-Unknown}"
ok "User: ${EREADER_USER}"
ok "Install dir: ${INSTALL_DIR}"
ok "Books dir: ${BOOKS_DIR}"

if ! confirm "Ready to set up the e-reader?"; then
    echo "Aborted."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: System update
# ---------------------------------------------------------------------------

step "Updating system packages"

sudo apt-get update -qq
ok "Package lists updated"

sudo apt-get upgrade -y -qq
ok "System packages upgraded"

# ---------------------------------------------------------------------------
# Step 2: Enable SPI and I2C
# ---------------------------------------------------------------------------

step "Enabling SPI and I2C interfaces"

SPI_CHANGED=false
I2C_CHANGED=false

# Enable SPI
if sudo raspi-config nonint get_spi | grep -q "1"; then
    sudo raspi-config nonint do_spi 0
    SPI_CHANGED=true
    ok "SPI enabled"
else
    ok "SPI already enabled"
fi

# Enable I2C
if sudo raspi-config nonint get_i2c | grep -q "1"; then
    sudo raspi-config nonint do_i2c 0
    I2C_CHANGED=true
    ok "I2C enabled"
else
    ok "I2C already enabled"
fi

# ---------------------------------------------------------------------------
# Step 3: Install system dependencies
# ---------------------------------------------------------------------------

step "Installing system dependencies"

PACKAGES=(
    python3-pip
    python3-dev
    python3-pil
    python3-numpy
    libopenjp2-7
    libtiff6
    libatlas-base-dev
    fonts-dejavu-core
    fonts-freefont-ttf
    fonts-liberation
    git
    wget
)

sudo apt-get install -y -qq "${PACKAGES[@]}"
ok "System packages installed"

# ---------------------------------------------------------------------------
# Step 4: Install Python packages
# ---------------------------------------------------------------------------

step "Installing Python packages"

pip install --break-system-packages --quiet inky[rpi] Pillow RPi.GPIO
ok "inky, Pillow, RPi.GPIO installed"

# Also install epub2txt dependencies so the Pi can convert locally if wanted
pip install --break-system-packages --quiet beautifulsoup4 lxml 2>/dev/null && \
    ok "beautifulsoup4, lxml installed (for epub2txt)" || \
    warn "beautifulsoup4/lxml install failed (epub2txt will use fallback parser)"

# ---------------------------------------------------------------------------
# Step 5: Download e-reader scripts
# ---------------------------------------------------------------------------

step "Downloading e-reader scripts"

mkdir -p "${INSTALL_DIR}"
mkdir -p "${BOOKS_DIR}"

# Try downloading from the configured repo URL first.
# If that fails (e.g. placeholder URL), fall back to writing embedded copies.

download_ok=true

_try_download() {
    local url="$1" dest="$2" name="$3"
    if curl -fsSL --connect-timeout 10 "${url}" -o "${dest}" 2>/dev/null; then
        # Sanity check — make sure we got a Python file, not a 404 HTML page
        if head -5 "${dest}" | grep -q "python\|#!/\|import\|\"\"\""; then
            ok "Downloaded ${name}"
            return 0
        fi
    fi
    return 1
}

if ! _try_download "${EREADER_URL}" "${INSTALL_DIR}/ereader.py" "ereader.py"; then
    download_ok=false
fi

if ! _try_download "${EPUB2TXT_URL}" "${INSTALL_DIR}/epub2txt.py" "epub2txt.py"; then
    download_ok=false
fi

if [[ "${download_ok}" == false ]]; then
    warn "Could not download from ${REPO_BASE}"
    echo ""
    info "This probably means you need to update the REPO_BASE URL"
    info "at the top of this script to point to your actual repository."
    echo ""
    info "For now, you can manually copy the scripts to: ${INSTALL_DIR}/"
    echo ""
    info "  scp ereader.py epub2txt.py ${EREADER_USER}@$(hostname).local:${INSTALL_DIR}/"
    echo ""
fi

chmod +x "${INSTALL_DIR}"/*.py 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 6: Download a sample book
# ---------------------------------------------------------------------------

step "Downloading a sample book"

SAMPLE_BOOK="${BOOKS_DIR}/alice_in_wonderland.txt"

if [[ -f "${SAMPLE_BOOK}" ]]; then
    ok "Sample book already exists"
else
    if wget -q --timeout=15 -O "${SAMPLE_BOOK}" \
        "https://www.gutenberg.org/cache/epub/11/pg11.txt" 2>/dev/null; then
        SIZE=$(du -h "${SAMPLE_BOOK}" | cut -f1)
        ok "Downloaded Alice in Wonderland (${SIZE})"
    else
        warn "Could not download sample book (no internet or Gutenberg is down)"
        info "Add .txt files to ${BOOKS_DIR}/ manually"
    fi
fi

# ---------------------------------------------------------------------------
# Step 7: Create systemd service
# ---------------------------------------------------------------------------

step "Setting up auto-start service"

SERVICE_FILE="/etc/systemd/system/ereader.service"

sudo tee "${SERVICE_FILE}" > /dev/null << UNIT
[Unit]
Description=Inky Impression E-Reader
After=multi-user.target

[Service]
Type=simple
User=${EREADER_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/ereader.py ${BOOKS_DIR}/
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable ereader.service
ok "ereader.service created and enabled"
info "It will start automatically on next boot"

# ---------------------------------------------------------------------------
# Step 8: Configure GPIO wake from suspend
# ---------------------------------------------------------------------------

step "Configuring GPIO wake from suspend"

BOOT_CONFIG="/boot/firmware/config.txt"
# Older Pi OS uses /boot/config.txt
if [[ ! -f "${BOOT_CONFIG}" ]]; then
    BOOT_CONFIG="/boot/config.txt"
fi

OVERLAY_LINE="dtoverlay=gpio-shutdown,gpio_pin=${WAKE_GPIO},active_low=1,gpio_pull=up"

if grep -q "gpio-shutdown" "${BOOT_CONFIG}" 2>/dev/null; then
    ok "GPIO wake overlay already configured"
else
    echo "" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    echo "# Inky E-Reader: wake from suspend on button A (GPIO ${WAKE_GPIO})" | \
        sudo tee -a "${BOOT_CONFIG}" > /dev/null
    echo "${OVERLAY_LINE}" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    ok "Added gpio-shutdown overlay (GPIO ${WAKE_GPIO}) to ${BOOT_CONFIG}"
fi

# ---------------------------------------------------------------------------
# Step 9: Passwordless sudo for suspend
# ---------------------------------------------------------------------------

step "Configuring passwordless suspend"

SUDOERS_FILE="/etc/sudoers.d/ereader"

if [[ -f "${SUDOERS_FILE}" ]]; then
    ok "Sudoers rule already exists"
else
    echo "${EREADER_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend" | \
        sudo tee "${SUDOERS_FILE}" > /dev/null
    sudo chmod 0440 "${SUDOERS_FILE}"
    ok "Passwordless suspend configured for ${EREADER_USER}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║                    Setup Complete!                       ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}E-reader:${NC}     ${INSTALL_DIR}/ereader.py"
echo -e "  ${BOLD}EPUB converter:${NC} ${INSTALL_DIR}/epub2txt.py"
echo -e "  ${BOLD}Books folder:${NC}  ${BOOKS_DIR}/"
echo -e "  ${BOLD}Service:${NC}       ereader.service (enabled, starts on boot)"
echo -e "  ${BOLD}Wake button:${NC}   GPIO ${WAKE_GPIO} (Button A)"
echo ""

if [[ "${download_ok}" == false ]]; then
    echo -e "  ${YELLOW}${BOLD}ACTION NEEDED:${NC} Copy ereader.py and epub2txt.py into ${INSTALL_DIR}/"
    echo -e "  From your main PC:"
    echo -e "    scp ereader.py epub2txt.py ${EREADER_USER}@$(hostname).local:${INSTALL_DIR}/"
    echo ""
fi

echo -e "  ${BOLD}Adding books from your PC:${NC}"
echo -e "    scp mybook.txt ${EREADER_USER}@$(hostname).local:${BOOKS_DIR}/"
echo ""
echo -e "  ${BOLD}Manual commands:${NC}"
echo -e "    sudo systemctl start ereader     # start now"
echo -e "    sudo systemctl stop ereader      # stop"
echo -e "    sudo systemctl status ereader    # check status"
echo -e "    journalctl -u ereader -f         # view logs"
echo ""

if confirm "Reboot now to apply SPI/I2C and GPIO changes?"; then
    echo ""
    echo -e "${CYAN}Rebooting in 3 seconds…${NC}"
    sleep 3
    sudo reboot
else
    echo ""
    warn "Remember to reboot before using the e-reader!"
    echo -e "  Run ${BOLD}sudo reboot${NC} when you're ready."
fi
