#!/bin/bash
# Boot the isolated desktop: Xvfb :1 → openbox → tint2 panel → chromium.
set -e
# XGA on purpose — the model's click grounding is calibrated around 1024x768;
# keep in sync with sandbox_container.GEOMETRY
Xvfb :1 -screen 0 1024x768x24 -nolisten tcp &
# clients (openbox/tint2/chromium/scrot/xdotool) all read DISPLAY from env —
# openbox 3.6 has NO --display flag (it exits on one, leaving a WM-less black
# screen; that exact bug shipped once).
export DISPLAY=:1
for i in $(seq 1 40); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.25; done
xsetroot -solid "#1c2230" || true
openbox &
sleep 0.5
# one virtual desktop only — openbox defaults to 4 and a wheel-scroll on the
# root window silently switches, making every open app "vanish" mid-run
xdotool set_num_desktops 1 || true
# no -c flag: tint2 generates its default taskbar config (a /dev/null config
# renders nothing at all)
tint2 >/dev/null 2>&1 &
# boot with a visible app so a fresh sandbox never reads as a dead feed
chromium --no-sandbox --no-first-run --start-maximized https://www.google.com >/dev/null 2>&1 &
# keep the container alive
sleep infinity
