#!/usr/bin/env bash
# One-time native input permissions for the active desktop user, plus dependencies.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi
if command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip python3-devel gcc kernel-headers wl-clipboard
elif command -v apt-get >/dev/null; then
    apt-get update
    apt-get install -y python3 python3-venv python3-pip python3-dev gcc linux-libc-dev wl-clipboard
else
    echo "Install Python 3.11+, pip, venv, development headers, gcc and wl-clipboard with your package manager."
fi
modprobe uinput
install -d /etc/modules-load.d /etc/udev/rules.d
cat > /etc/modules-load.d/lan-mouse-python.conf <<'EOF'
uinput
EOF
# Run before systemd's 73-seat-late.rules, which grants ACLs to the active seat user.
cat > /etc/udev/rules.d/70-lan-mouse-python.rules <<'EOF'
SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess"
SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_MOUSE}=="1", TAG+="uaccess"
SUBSYSTEM=="misc", KERNEL=="uinput", TAG+="uaccess"
EOF
udevadm control --reload-rules
udevadm trigger --subsystem-match=input
udevadm trigger --subsystem-match=misc --sysname-match=uinput
udevadm settle
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    # Allow this application and mDNS only from local IPv4 network ranges.
    mapfile -t lanmouse_zones < <(firewall-cmd --get-active-zones | awk '/^[^ ]/{print $1}')
    if [[ ${#lanmouse_zones[@]} -eq 0 ]]; then
        lanmouse_zones+=("$(firewall-cmd --get-default-zone)")
    fi
    for lanmouse_zone in "${lanmouse_zones[@]}"; do
        for lanmouse_source in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
            for lanmouse_port in '45831 tcp' '5353 udp'; do
                read -r lanmouse_number lanmouse_protocol <<< "$lanmouse_port"
                lanmouse_rule="rule family=\"ipv4\" source address=\"$lanmouse_source\" port port=\"$lanmouse_number\" protocol=\"$lanmouse_protocol\" accept"
                firewall-cmd --zone="$lanmouse_zone" --add-rich-rule="$lanmouse_rule" >/dev/null
                firewall-cmd --permanent --zone="$lanmouse_zone" --add-rich-rule="$lanmouse_rule" >/dev/null
            done
        done
    done
    echo "Allowed LAN Mouse and discovery through firewalld for local network addresses."
fi
echo "Setup complete. Run ./start.sh as your normal desktop user."
echo "If input is still unavailable, log out and back in once."
echo "If discovery is blocked, allow TCP 45831 and mDNS UDP 5353 on your trusted LAN."
