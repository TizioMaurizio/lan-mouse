# Verification

Local environment: Fedora KDE Plasma on Wayland, 7 October 2026.
Tested interpreters: Python 3.12 and the laptop's Python 3.14.3.
Qt: PySide6 6.11.2.

Verified locally:

- **78 tests passed**, with the Windows-only backend module skipped on Linux.
- Automated Python test suite on Linux, including live loopback TLS connections.
- GUI starts and renders. Missing `/dev/uinput` permissions produce a setup
  message and leave physical input untouched.
- KDE's actual D-Bus service accepts the edge script, starts it, and removes
  it on cleanup. Physical screen crossing has not been exercised.
- Clipboard text/image handling and suppression of feedback loops with an
  isolated Qt clipboard.
- Wayland clipboard adapter send/receive and feedback suppression using mocked
  wl-clipboard operations, without touching the user's actual clipboard.
- Bash launcher/setup syntax, Python compilation, package installation and Ruff.
- Linux setup applied on the Fedora 44 laptop: build dependencies installed,
  udev permissions applied, and LAN firewall rules added.
- Native Linux input initialization succeeded using the laptop's Python 3.14.3:
  uinput keyboard/mouse devices opened and three physical input devices were
  accessible. No physical input was grabbed during this initialization check.
- KDE cursor position notifications reach the D-Bus adapter on the actual laptop.
  A virtual absolute pointer moved to the requested position across the complete
  3840 × 1080 desktop, then restored the original pointer position.
- Large PNG images retain their pixels through chunked loopback TLS transfers.
  A paced socket test verifies that keyboard input arrives between chunks of
  a multi-megabyte clipboard item, ahead of the remaining clipboard data.
- Movement coalescing preserves displacement, button/key ordering and control
  stamps. Both TLS peers enable TCP_NODELAY.
- Mixed browser image/URL clipboard offers and Wayland image conversion;
  background Qt image compression; stale and duplicate clipboard notifications.
- Immediate edge crossing/rearming, height-preserving entry, suppression of
  stale border reports, and rapid reverse handoffs are covered by isolated tests.
- KWin service replacement releases input, reconnects without pausing, clears
  stale screen geometry, and reloads the edge script. Ordinary disconnects also
  reset the outgoing edge flag, so the next connection can switch screens.

Not yet verified:

- Native Windows input hooks/SendInput and physical Linux evdev grabs/uinput
  injection between two real computers. Tests for both backends use mock devices.
- Windows launch/installation on a real Windows PC. Windows-specific tests are
  included for the GitHub Actions Windows runner.
- Clipboard data-control integration against the real Wayland clipboard.

## Physical check after installation

1. Run the Linux setup once and open the app on both PCs.
2. Connect and approve on both PCs. Verify both screens report connected.
3. With the Linux mouse/keyboard, press F8, move/click/scroll in Windows and type
   a short line. Test Shift, Ctrl+C/Ctrl+V, Alt+Tab, arrows and a held letter.
4. Press F8 to return. Repeat using the Windows devices to control Linux.
5. Test the joining screen edge in both directions. F8 should always return
   control immediately when the originating mouse/keyboard is being shared.
6. Copy Unicode text, a screenshot, and a browser image in each direction.
   Paste into an image editor or chat app. Move/type on the other PC while copying
   a large image. Confirm a newer local copy is not replaced by a pending image.
   Cross the joining edge repeatedly in both directions, including fast reversals,
   and confirm entry height is preserved without getting stuck at the border.
7. While controlling the remote PC, stop the receiver app or disconnect Wi-Fi.
   Confirm local input returns within about eight seconds. Pressing F8 should
   return sooner. Restart/reconnect and confirm no approval prompt repeats.
8. Close the app on both PCs and check that each keyboard and mouse works normally.

Use ordinary desktop apps for the first check. Windows elevation and secure
desktop behavior are documented limitations.
