"""KDE Wayland window rule for the tray's optional settings window."""

import os
import shutil
import subprocess
import sys

APP_ID = "org.lanmouse.LANMouse"
RULE_ID = "793b99d4-730b-4bdd-bde8-07ee37f2e1f6"


def configure_kde_taskbar(tray_available):
    # Wayland has no generic skip-taskbar flag; Qt.Tool alone is insufficient
    # on KDE. Keep an app-specific KWin rule, preserving every other rule.
    if (
        sys.platform != "linux"
        or not os.environ.get("WAYLAND_DISPLAY")
        or "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "")
    ):
        return False
    reader, writer = shutil.which("kreadconfig6"), shutil.which("kwriteconfig6")
    dbus = next(
        (path for name in ("qdbus-qt6", "qdbus6", "qdbus") if (path := shutil.which(name))), None
    )
    if not reader or not writer or not dbus:
        return False

    def run(command):
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=3)

    def read(group, key):
        return run([reader, "--file", "kwinrulesrc", "--group", group, "--key", key]).stdout.strip()

    def write(group, key, value):
        run([writer, "--file", "kwinrulesrc", "--group", group, "--key", key, str(value)])

    try:
        changed = False
        values = {
            "Description": "LAN Mouse - tray settings window",
            "wmclass": APP_ID,
            "wmclassmatch": "1",  # Exact application ID.
            "wmclasscomplete": "false",
            "types": "4294967295",
            "skiptaskbar": "true" if tray_available else "false",
            "skiptaskbarrule": "2" if tray_available else "1",  # Force / Don't affect.
            "skipswitcher": "true" if tray_available else "false",
            "skipswitcherrule": "2" if tray_available else "1",
        }
        for key, value in values.items():
            if read(RULE_ID, key) != value:
                write(RULE_ID, key, value)
                changed = True
        rules = [rule for rule in read("General", "rules").split(",") if rule]
        if RULE_ID not in rules:
            rules.append(RULE_ID)
            write("General", "rules", ",".join(rules))
            changed = True
        if changed:
            run([dbus, "org.kde.KWin", "/KWin", "org.kde.KWin.reconfigure"])
        return True
    except (OSError, subprocess.SubprocessError):
        # Tray hiding still works when a distribution lacks KDE config tools.
        return False
