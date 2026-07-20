#!/bin/bash
# Boot the isolated desktop: Xvfb :1 → dbus → a full XFCE4 session → chromium.
set -e
# Compact 5:4 geometry; keep in sync with sandbox_container.GEOMETRY;
# keep in sync with sandbox_container.GEOMETRY
# A stale X lock survives an unclean stop (same persistence class as the
# chromium SingletonLock below) and makes Xvfb refuse to start ("Server is
# already active for display 1") -> the whole desktop wedges on restart.
# Clear it -- it's only meaningful within one boot.
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
Xvfb :1 -screen 0 960x768x24 -nolisten tcp &
# clients all read DISPLAY from env — never pass a --display flag (openbox 3.6
# had none and died on one; that exact bug shipped once)
export DISPLAY=:1
for i in $(seq 1 40); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.25; done

# XFCE preferences BEFORE the session starts (first boot only — xfconf files
# persist in the container layer afterwards and the user may change them):
# Greybird + elementary icons (the xubuntu look), ONE workspace (stock is 4 and
# a wheel-scroll on the desktop silently switched — every open app "vanished"
# mid-run), and no session-save prompts on logout.
CFG="$HOME/.config/xfce4/xfconf/xfce-perchannel-xml"
if [ ! -f "$CFG/xsettings.xml" ]; then
  mkdir -p "$CFG"
  cat > "$CFG/xsettings.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xsettings" version="1.0">
  <property name="Net" type="empty">
    <property name="ThemeName" type="string" value="Greybird"/>
    <property name="IconThemeName" type="string" value="elementary-xfce-dark"/>
  </property>
</channel>
EOF
  cat > "$CFG/xfwm4.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfwm4" version="1.0">
  <property name="general" type="empty">
    <property name="theme" type="string" value="Greybird"/>
    <property name="workspace_count" type="int" value="1"/>
  </property>
</channel>
EOF
fi

# the standard user dirs — xfdesktop shows ~/Desktop; Transfer (the cockpit's
# file exchange) reads/writes Downloads/Desktop/Documents
mkdir -p "$HOME/Desktop" "$HOME/Downloads" "$HOME/Documents"

# the full desktop session: xfwm4 + panel + xfdesktop (wallpaper, icons)
dbus-launch --exit-with-session startxfce4 &
# wait for the window manager before launching apps
for i in $(seq 1 40); do xdotool search --class xfdesktop >/dev/null 2>&1 && break; sleep 0.25; done
# belt-and-braces: pin to one workspace even if an old xfconf survives
xdotool set_num_desktops 1 || true
# The home volume PERSISTS across container restarts, and so does chromium's
# SingletonLock — it points at the *previous* run's PID+hostname. On restart
# that PID is gone but chromium sees the lock and refuses to start ("profile
# appears to be in use by another Chromium process on another computer") → the
# in-VM browser silently never launches. Clear the stale lock on every boot;
# it's only meaningful within one live session.
rm -f "$HOME/.config/chromium/Singleton"* 2>/dev/null || true
# boot with a visible app so a fresh sandbox never reads as a dead feed
chromium --no-sandbox --test-type --no-first-run --start-maximized https://www.google.com >/dev/null 2>&1 &
# keep the container alive
sleep infinity
