# LAN Mouse — Python

Share your keyboard, mouse and clipboard between a Linux PC and a Windows PC
on the same local network. Run the same app on both computers.

Choose the other computer and approve the connection once on each screen.
There are no security keys, pairing codes, accounts or cloud services to set up.
Paired computers reconnect automatically while the app is open.

This is an independent Python app, not Microsoft's Mouse Without Borders or
the Rust project also named Lan Mouse. It connects only to another copy of this app.

## Start on Linux

From this project folder:

```bash
./setup-linux.sh
./start.sh
```

The setup script asks for your sudo password once. It installs Python build
dependencies and `wl-clipboard`, loads `uinput`, and grants the active desktop
user access to mouse/keyboard input through udev. If firewalld is running, it
allows the app's TCP port and mDNS discovery from local IPv4 network ranges.
It supports Fedora and Ubuntu/Debian package managers. On other distributions,
install the listed dependencies with your package manager first.

Run the app as your normal desktop user. If input still reports unavailable,
log out and back in once, then reopen it. The setup script has been run on the
development laptop with Fedora 44 KDE; its keyboard/mouse and uinput access
checks passed without logging out.

## Start on Windows

1. Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/windows/).
   Enable **Add python.exe to PATH** during installation.
2. Clone your GitHub repository, or copy this whole project folder to Windows.
3. Double-click **start.cmd**.
4. If Windows Firewall asks about Python, allow it on your **Private network**.

Both launchers create a local `.venv` and install dependencies automatically
on the first run. Later starts reuse that environment. The first installation
downloads Qt and may take a few minutes.

## Connect and use

1. Open LAN Mouse on both PCs.
2. On either PC, select the other computer under **Nearby computers** and click
   **Connect**. Approve the connection on both computers.
3. Set **The other computer is on my Right/Left** to match your desk on each PC.
   For example: Linux = Right, Windows = Left when Windows sits to Linux's right.
4. Move the pointer to that edge to control the other computer. Crossing switches
   immediately, including on KDE Wayland. The pointer enters the other screen
   just inside its joining edge, at the same relative height. Alternatively, press
   **F8** or click **Control the other computer**.
5. Move to the joining edge on the receiving PC, or press **F8** again, to return
   to local control. F8 on either PC returns both PCs to local control.
6. Copy and paste normally. Plain text and images synchronize in either
   direction, even when you are not currently sharing the mouse.

From local mode, either computer's physical keyboard and mouse can control the
other. Release held keys and mouse buttons before switching. Use a physical
relative mouse on Linux, such as a USB/Bluetooth mouse.

If discovery does not find the other PC, enter its IPv4 address in **Connect by
IP**. Each app displays its own IP at the top. This needs no router port forwarding.
The computer that initiates a connection supplies the initial clipboard content;
later copies on either computer update both clipboards.

Image compression runs in the background. Clipboard transfers use small chunks
interleaved with mouse/keyboard input; they do not occupy the connection as one
large message. Mouse movements combine adjacent bursts at up to 500 updates per
second while preserving key, click, scroll and handoff ordering.

## Updating an existing installation

Close LAN Mouse, run `git pull` in this folder on **both PCs**, then run
`start.sh` on Linux or `start.cmd` on Windows again. Version 0.2 changes the wire
protocol to support clipboard chunks, so both PCs must be updated. Saved
pairings remain valid; you do not need to approve the computers again.

The app stays running when minimized. Closing its window or choosing **Quit**
stops sharing. **Disconnect** pauses reconnection on that PC until you click
Connect again. After an unexpected drop, it reconnects and leaves control local;
press F8 to resume remote control.

## Linux desktop support

| Desktop | Mouse/keyboard | Clipboard | Screen edges |
| --- | --- | --- | --- |
| KDE Plasma on Wayland | Native evdev/uinput | wl-clipboard | Temporary KWin script |
| X11 desktops | Native evdev/uinput | Qt clipboard | Outside desktop edge |
| Other Wayland desktops | Native evdev/uinput | Requires wl-clipboard data-control support | Use F8 |
| Windows | Native hooks/SendInput | Qt clipboard | Outside desktop edge |

The KWin script is loaded only while connected with screen switching enabled,
and unloaded when disconnected or closed. It is not installed as a permanent
KWin extension. It observes cursor movement directly, without KDE's screen-edge
activation delay. Joining edges rearm as soon as the pointer moves inward, so
you can cross back immediately. KDE Wayland uses a virtual absolute pointer
for precise placement without mouse acceleration.

## Current limits

- Two computers per connection; one controls the other at a time.
- Clipboard: plain UTF-8 text and images, at most **8 MiB** per item and
  32 million pixels per image. Screenshots and browser **Copy image** work in
  either direction, including image offers that also contain a URL. Native
  Windows images and supported Wayland PNG/JPEG/WebP/BMP/TIFF offers are
  transferred as PNG. Rich text formatting and file transfers are not included.
- Linux absolute devices (touchpads, tablets and touchscreens) are not forwarded.
  Use a relative mouse. An absolute device may still move the local pointer.
- Common PC keyboard keys, modifiers, navigation, F1–F12, and common media keys
  are mapped by physical key position. Use matching keyboard layouts on both
  PCs. Some uncommon hardware keys and IME combinations may need extra mapping.
- F8 is reserved for switching while connected. On Linux its local press may
  also reach the focused application when input is not being shared.
- Windows secure desktops, UAC prompts, Ctrl+Alt+Delete and sign-in screens are
  outside the app's scope. Elevated Windows apps may require starting LAN Mouse
  as administrator.
- IPv4 local networks. Guest Wi-Fi/client isolation may prevent communication.
- Automated checks run on Linux and Windows. Physical two-PC behavior,
  including timing on your network, still needs a check after each update.

## Connection and trust

Traffic is encrypted with TLS 1.2 or newer. Each installation creates its own
local identity. First pairing uses explicit name/IP approval on each computer;
future connections pin that exact identity. The client proves possession of
its private key with a fresh, server-bound signed challenge. This keeps the
connection simple without passing codes between screens.

First pairing should take place on a trusted LAN: name/IP approval does not
provide independent verification against a machine impersonating your peer
during that initial pairing. No input or clipboard is shared before pairing
completes. There is no screen capture, shell execution or telemetry.

Settings and identities live outside the project:

- Linux: `~/.config/lan-mouse-python/` (or under `XDG_CONFIG_HOME`).
- Windows: `%LOCALAPPDATA%\lan-mouse-python\`.

To forget pairings, close the app, remove the `peers` entries in `settings.json`
on each PC, then reopen it. Do not copy `identity.key`, `identity.pem`, or that
settings directory to GitHub or the other PC. The project's `.gitignore`
excludes identity files and virtual environments.

Network: **TCP 45831**, discovery **mDNS UDP 5353**. If discovery is blocked,
Connect by IP still works when TCP 45831 is reachable. With another firewall
besides firewalld, allow these ports on the trusted local network yourself.

## Development and checks

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

Tests exercise real TLS sockets, pairing denial, certificate changes, automatic
reconnect by saved IP, framing limits, Unicode text, image clipboard feedback,
control handoff, stale input rejection, device-grab rollback, hotkey recovery,
disconnect recovery, browser image offers, lossless chunked image transfers,
input priority during large clipboard transfers, and immediate edge rearming. Desktop tests use an isolated Qt clipboard; backend
tests mock hardware so they never take over the developer's input devices.

The included GitHub Actions workflow runs these checks on Linux and Windows
with Python 3.11 and 3.13 when you publish the repository.

MIT license. See [TESTING.md](TESTING.md) for what was verified locally and the
short physical two-PC check.
