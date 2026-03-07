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
#   2. Enables SPI (with spi0-0cs overlay) and I2C interfaces
#   3. Installs system dependencies and fonts
#   4. Installs Python packages (inky, Pillow, RPi.GPIO)
#   5. Downloads ereader.py and epub2txt.py (600×400 optimized)
#   6. Creates ~/books/ with a sample book from Project Gutenberg
#   7. Sets up a systemd service for auto-start on boot
#   8. Removes GPIO shutdown overlay (prevents unwanted shutdowns)
#   9. Reboots to apply SPI/I2C changes
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

info "Updating package lists..."
if sudo apt-get update; then
    ok "Package lists updated"
else
    fail "Failed to update package lists"
    exit 1
fi

info "Upgrading system packages (this may take a few minutes)..."
if sudo apt-get upgrade -y; then
    ok "System packages upgraded"
else
    warn "Package upgrade had issues, but continuing..."
fi

# ---------------------------------------------------------------------------
# Step 2: Enable SPI and I2C with proper overlay
# ---------------------------------------------------------------------------

step "Enabling SPI and I2C interfaces"

SPI_CHANGED=false
I2C_CHANGED=false

BOOT_CONFIG="/boot/firmware/config.txt"
# Older Pi OS uses /boot/config.txt
if [[ ! -f "${BOOT_CONFIG}" ]]; then
    BOOT_CONFIG="/boot/config.txt"
fi

# Enable SPI with spi0-0cs overlay to avoid GPIO conflicts
info "Configuring SPI interface..."
if [[ -f "${BOOT_CONFIG}" ]]; then
    if grep -q "dtoverlay=spi0-0cs" "${BOOT_CONFIG}" 2>/dev/null; then
        ok "SPI overlay already configured"
    else
        info "Adding SPI overlay to ${BOOT_CONFIG}..."
        # Remove old SPI config if present
        sudo sed -i '/^dtparam=spi=on/d' "${BOOT_CONFIG}" 2>/dev/null || true
        # Add new overlay
        if echo "" | sudo tee -a "${BOOT_CONFIG}" > /dev/null && \
           echo "# Inky E-Reader: SPI with no chip select conflicts" | sudo tee -a "${BOOT_CONFIG}" > /dev/null && \
           echo "dtoverlay=spi0-0cs" | sudo tee -a "${BOOT_CONFIG}" > /dev/null; then
            SPI_CHANGED=true
            ok "SPI overlay configured"
        else
            warn "Failed to add SPI overlay"
        fi
    fi
else
    warn "Could not find boot config file"
fi

# Enable I2C
info "Checking I2C interface..."
if sudo raspi-config nonint get_i2c | grep -q "1"; then
    info "Enabling I2C..."
    if sudo raspi-config nonint do_i2c 0; then
        I2C_CHANGED=true
        ok "I2C enabled"
    else
        warn "Failed to enable I2C (not critical)"
    fi
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
    fonts-dejavu-core
    fonts-freefont-ttf
    fonts-liberation
    git
    wget
)

info "Installing: ${PACKAGES[*]}"
info "This may take a few minutes..."
if sudo apt-get install -y "${PACKAGES[@]}"; then
    ok "System packages installed"
else
    fail "Failed to install system packages"
    info "Try running manually: sudo apt-get install -y ${PACKAGES[*]}"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: Install Python packages
# ---------------------------------------------------------------------------

step "Installing Python packages"

info "Installing inky[rpi], Pillow, RPi.GPIO..."
if pip install --break-system-packages inky[rpi] Pillow RPi.GPIO; then
    ok "inky, Pillow, RPi.GPIO installed"
else
    fail "Failed to install Python packages"
    info "Try running manually: pip install --break-system-packages inky[rpi] Pillow RPi.GPIO"
    exit 1
fi

# Also install epub2txt dependencies so the Pi can convert locally if wanted
info "Installing beautifulsoup4, lxml (for epub2txt)..."
if pip install --break-system-packages beautifulsoup4 lxml 2>/dev/null; then
    ok "beautifulsoup4, lxml installed (for epub2txt)"
else
    warn "beautifulsoup4/lxml install failed (epub2txt will use fallback parser)"
fi

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

if [[ -f "${INSTALL_DIR}/ereader.py" ]]; then
    chmod +x "${INSTALL_DIR}/ereader.py"
    ok "ereader.py is ready"
else
    warn "ereader.py not found at ${INSTALL_DIR}/ereader.py"
fi

if [[ -f "${INSTALL_DIR}/epub2txt.py" ]]; then
    chmod +x "${INSTALL_DIR}/epub2txt.py"
    ok "epub2txt.py is ready"
else
    warn "epub2txt.py not found at ${INSTALL_DIR}/epub2txt.py"
fi

# ---------------------------------------------------------------------------
# Step 6: Download a sample book
# ---------------------------------------------------------------------------

step "Downloading a sample book"

SAMPLE_BOOK="${BOOKS_DIR}/alice_in_wonderland.txt"

if [[ -f "${SAMPLE_BOOK}" ]]; then
    ok "Sample book already exists"
else
    info "Downloading Alice in Wonderland from Project Gutenberg..."
    if wget --timeout=15 -O "${SAMPLE_BOOK}" \
        "https://www.gutenberg.org/cache/epub/11/pg11.txt" 2>&1 | grep -v "^--"; then
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

info "Creating systemd service file..."
if sudo tee "${SERVICE_FILE}" > /dev/null << UNIT
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
then
    ok "Service file created"
else
    fail "Failed to create service file"
    exit 1
fi

info "Reloading systemd daemon..."
if sudo systemctl daemon-reload; then
    ok "Systemd daemon reloaded"
else
    fail "Failed to reload systemd"
    exit 1
fi

info "Enabling ereader.service..."
if sudo systemctl enable ereader.service; then
    ok "ereader.service enabled"
    info "It will start automatically on next boot"
else
    warn "Failed to enable service (you can try manually later)"
fi

# ---------------------------------------------------------------------------
# Step 8: Cleanup old GPIO shutdown overlay (if present)
# ---------------------------------------------------------------------------

step "Removing GPIO shutdown overlay (not needed)"

BOOT_CONFIG="/boot/firmware/config.txt"
# Older Pi OS uses /boot/config.txt
if [[ ! -f "${BOOT_CONFIG}" ]]; then
    BOOT_CONFIG="/boot/config.txt"
fi

if [[ -f "${BOOT_CONFIG}" ]]; then
    if grep -q "gpio-shutdown" "${BOOT_CONFIG}" 2>/dev/null; then
        info "Removing old GPIO shutdown overlay..."
        # Comment out or remove gpio-shutdown lines
        sudo sed -i 's/^dtoverlay=gpio-shutdown/# dtoverlay=gpio-shutdown (disabled - causes unwanted shutdowns)/' "${BOOT_CONFIG}"
        sudo sed -i '/Inky E-Reader: wake from suspend/s/^/# /' "${BOOT_CONFIG}"
        ok "GPIO shutdown overlay disabled"
    else
        ok "No GPIO shutdown overlay found (good)"
    fi
fi

# Note: We don't configure passwordless suspend since the e-reader
# now uses a simple sleep screen instead of system suspend

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
echo -e "  ${BOLD}Display:${NC}       Optimized for 600×400 Inky Impression"
echo ""

if [[ "${download_ok}" == false ]]; then
    echo -e "  ${YELLOW}${BOLD}ACTION NEEDED:${NC} Copy ereader.py and epub2txt.py into ${INSTALL_DIR}/"
    echo -e "  From your main PC:"
    echo -e "    scp ereader.py epub2txt.py ${EREADER_USER}@$(hostname).local:${INSTALL_DIR}/"
    echo ""
fi

echo -e "  ${BOLD}Button Controls (when reading):${NC}"
echo -e "    Button A (GPIO 5)   - Next page"
echo -e "    Button B (GPIO 6)   - Previous page"
echo -e "    Button C (GPIO 16)  - Menu"
echo -e "    Button D (GPIO 24)  - Full refresh (clear ghosting)"
echo ""
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
