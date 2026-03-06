# Inky_Reader

A full-featured e-reader for the Pimoroni Inky Impression 4" seven-colour e-ink display, built with Python and Pillow.

![Library Browser](sim_000_browser.png)
![Reading View](sim_002_page_0001.png)

---

## Features

- **Library browser** — scans a folder of `.txt` files and presents them as a scrollable list
- **Paginated reading view** — clean serif text on white, with progress bar and page counter
- **Automatic bookmarking** — saves your position on every page turn and restores it when you reopen
- **Deep sleep** — suspends the Pi to save power; wakes on button press
- **Simulation mode** — renders all screens as PNGs on any computer, no Pi required

## Button Controls

| Button | GPIO | Library | Menu | Reading |
|--------|------|---------|------|---------|
| **A** | 5 | Scroll up | Move up | Previous page |
| **B** | 6 | Open book | Confirm | Open menu |
| **C** | 16 | Refresh list | Back to reading | — |
| **D** | 24 | Scroll down | Move down | Next page |

---

## What You'll Need

- Raspberry Pi (Zero 2 W, 3, 4, or 5)
- Pimoroni Inky Impression 4" (640×400, 7-colour)
- microSD card (8 GB or larger)
- Power supply for your Pi
- Another computer to flash the SD card
- Wi-Fi or Ethernet for initial setup (SSH)

---

## Installation — From Blank SD Card

### 1. Flash Raspberry Pi OS

Download and install the **Raspberry Pi Imager** from [raspberrypi.com/software](https://www.raspberrypi.com/software/) on your main computer.

1. Insert your microSD card
2. Open Raspberry Pi Imager
3. Click **Choose OS** → **Raspberry Pi OS (other)** → **Raspberry Pi OS Lite (64-bit)**
   - Lite is ideal since we don't need a desktop environment
4. Click **Choose Storage** and select your SD card
5. Click the **gear icon** (or press Ctrl+Shift+X) to open advanced options:
   - **Enable SSH** — tick "Use password authentication"
   - **Set username and password** — e.g. username `pi`, pick a password
   - **Configure Wi-Fi** — enter your network name and password, set your country
   - **Set locale** — choose your timezone and keyboard layout
6. Click **Write** and wait for it to finish

### 2. First Boot

1. Insert the SD card into your Pi and power it on
2. Wait about 60–90 seconds for first boot to complete
3. Find your Pi's IP address (check your router's admin page, or try `ping raspberrypi.local` from your computer)
4. SSH in:

```bash
ssh pi@raspberrypi.local
```

### 3. Update the System

```bash
sudo apt update && sudo apt upgrade -y
```

Reboot if a kernel update was installed:

```bash
sudo reboot
```

Then SSH back in.

### 4. Enable SPI and I2C

The Inky display communicates over SPI, and the buttons are read via GPIO. Enable the required interfaces:

```bash
sudo raspi-config
```

Navigate to **Interface Options**:

- Enable **SPI**
- Enable **I2C**

Select **Finish** and reboot when prompted:

```bash
sudo reboot
```

### 5. Install System Dependencies

```bash
sudo apt install -y python3-pip python3-dev python3-pil python3-numpy \
    libopenjp2-7 libtiff6 libatlas-base-dev fonts-dejavu-core
```

> The `fonts-dejavu-core` package provides DejaVu Serif and DejaVu Sans, which the e-reader uses by default. If you want extra fallback fonts:
>
> ```bash
> sudo apt install -y fonts-freefont-ttf fonts-liberation
> ```

### 6. Install Python Packages

```bash
pip install inky[rpi] Pillow RPi.GPIO --break-system-packages
```

> The `--break-system-packages` flag is required on Raspberry Pi OS Bookworm and later, which uses externally managed Python environments by default.

### 7. Download the E-Reader

Create a directory for the project and copy the script into it:

```bash
mkdir -p ~/ereader
cd ~/ereader
```

Transfer `ereader.py` from your computer using SCP. On your main machine run:

```bash
scp ereader.py pi@raspberrypi.local:~/ereader/
```

Or if clone the Git repository:

```bash
git clone https://github.com/sp3lllz/Inky_Reader.git ~/ereader
```

Either way, make the script executable:

```bash
chmod +x ~/ereader/ereader.py
```

### 8. Add Some Books

The e-reader looks for `.txt` files in `~/books/` by default:

```bash
mkdir -p ~/books
```

You can grab public-domain books from [Project Gutenberg](https://www.gutenberg.org/) — download the **Plain Text UTF-8** versions:

```bash
cd ~/books
wget -O alice_in_wonderland.txt "https://www.gutenberg.org/cache/epub/11/pg11.txt"
wget -O pride_and_prejudice.txt "https://www.gutenberg.org/cache/epub/1342/pg1342.txt"
wget -O sherlock_holmes.txt "https://www.gutenberg.org/cache/epub/1661/pg1661.txt"
```

### 9. Test It

Attach the Inky Impression to your Pi's GPIO header and run:

```bash
cd ~/ereader
python3 ereader.py
```

You should see the Library Browser appear on the e-ink screen. Press the physical buttons to navigate.

> **Tip:** The Inky Impression 7-colour display takes about 30 seconds to fully refresh. This is normal for this type of e-ink panel — be patient after each button press.

---

## Optional Setup

### Run on Boot with systemd

Create a service file so the e-reader starts automatically when the Pi powers on:

```bash
sudo nano /etc/systemd/system/ereader.service
```

Paste the following:

```ini
[Unit]
Description=Inky Impression E-Reader
After=multi-user.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/ereader
ExecStart=/usr/bin/python3 /home/pi/ereader/ereader.py /home/pi/books/
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ereader.service
sudo systemctl start ereader.service
```

Check status:

```bash
sudo systemctl status ereader.service
```

To view logs:

```bash
journalctl -u ereader.service -f
```

### Configure GPIO Wake from Sleep

The e-reader's Sleep function calls `sudo systemctl suspend`. To allow a button press to wake the Pi back up, edit the boot config:

```bash
sudo nano /boot/firmware/config.txt
```

Add this line at the end (using Button A / GPIO 5 as the wake source):

```
dtoverlay=gpio-shutdown,gpio_pin=5,active_low=1,gpio_pull=up
```

Reboot for it to take effect:

```bash
sudo reboot
```

Now pressing Button A will wake the Pi from suspend.

### Allow Suspend Without a Password

The sleep function runs `sudo systemctl suspend`. To avoid needing a password, add a sudoers rule:

```bash
sudo visudo -f /etc/sudoers.d/ereader
```

Add this line:

```
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend
```

### Use a Custom Books Directory

Pass a different path as the first argument:

```bash
python3 ereader.py /media/usb/my-library/
```

The directory will be created automatically if it doesn't exist.

---

## Simulation Mode

You can preview all the screens on any computer (no Pi or display needed) as long as you have Python 3 and Pillow installed:

```bash
pip install Pillow
python3 ereader.py ~/books/ --simulate
```

This generates numbered PNG files in the current directory:

```
sim_000_browser.png    — Library browser
sim_001_menu.png       — Main menu
sim_002_page_0001.png  — First page of reading
sim_003_page_0002.png  — Second page
sim_004_page_0003.png  — Third page
sim_005_sleep.png      — Sleep screen
```

---

## File Structure

```
~/
├── ereader/
│   └── ereader.py            # The application (single file)
├── books/
│   ├── alice_in_wonderland.txt
│   ├── pride_and_prejudice.txt
│   └── ...
└── .ereader_saves.json        # Auto-generated bookmark file
```

## Troubleshooting

**Display not found / SPI errors**
- Confirm SPI is enabled: `ls /dev/spidev*` should show devices
- Check the display is firmly seated on the GPIO header
- Run `pip install inky[rpi] --break-system-packages` again to ensure the driver is installed

**Buttons not responding**
- The buttons are active-low on GPIOs 5, 6, 16, and 24
- Make sure nothing else is claiming those pins (check for conflicting overlays in `/boot/firmware/config.txt`)
- Test GPIO manually: `python3 -c "import RPi.GPIO as G; G.setmode(G.BCM); G.setup(5,G.IN,pull_up_down=G.PUD_UP); print(G.input(5))"`
  - Should print `1` normally, `0` when button A is held

**ImportError: No module named 'inky'**
- Reinstall: `pip install inky[rpi] --break-system-packages`

**Font looks blocky or wrong**
- Install fonts: `sudo apt install fonts-dejavu-core`
- The app falls back to Pillow's built-in bitmap font if no TTF is found — it works, but looks rough

**Suspend/wake not working**
- Ensure the `gpio-shutdown` overlay is in `/boot/firmware/config.txt` and you've rebooted
- Ensure the sudoers rule is in place (see above)
- If suspend isn't supported on your Pi model, the app falls back to waiting for a button press

**Books not appearing**
- Files must have a `.txt` extension
- Check the path: by default it's `~/books/`, but you can pass a different directory
- Press button C on the library screen to refresh

## Companion Script: epub2txt.py

A converter that turns DRM-free `.epub` files into clean `.txt` files ready to load onto the e-reader. Run this on your main PC.

### Install (on your PC)

```bash
pip install beautifulsoup4 lxml
```

> Both are optional — the script falls back to a standard-library regex parser without them, but BS4 + lxml gives much better results.

### Usage

```bash
# Single file
python3 epub2txt.py book.epub

# Multiple files
python3 epub2txt.py book1.epub book2.epub

# Every EPUB in a folder
python3 epub2txt.py ~/Downloads/epubs/

# Output to a specific directory
python3 epub2txt.py book.epub -o ~/converted/

# Convert and SCP straight to the Pi in one command
python3 epub2txt.py book.epub --scp pi@raspberrypi.local:~/books/

# Preview what would be converted without writing anything
python3 epub2txt.py ~/Downloads/epubs/ --dry-run
```

The converter reads the EPUB's spine for correct chapter order, extracts text from each XHTML document, preserves paragraph breaks and section dividers, and prepends the title and author from the book's metadata.

---

## Licence

Published under the MIT Licence
