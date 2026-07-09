# Isolated Linux desktop for the Operator sandbox surface — a full XFCE4
# desktop (panel + app menu + wallpaper + real file manager), not a bare WM.
# Runs its own Xvfb on :1; the host drives it via `docker exec` (a persistent
# ffmpeg MJPEG pipe for the live feed, a persistent shell for xdotool input,
# scrot for the agent's full-quality screenshots).
FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:1

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11-xserver-utils x11-utils xdotool scrot ffmpeg \
      dbus-x11 \
      xfce4 xfce4-terminal thunar mousepad ristretto galculator \
      greybird-gtk-theme elementary-xfce-icon-theme \
      desktop-base \
      chromium \
      fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy the boot script (as root) BEFORE dropping privileges.
COPY start-desktop.sh /usr/local/bin/start-desktop.sh
RUN chmod +x /usr/local/bin/start-desktop.sh

# Non-root desktop user (base image already has an "operator" GROUP, so give the
# user its own primary group via -U). The agent runs as this, never root.
RUN useradd -m -s /bin/bash -U opuser
USER opuser
WORKDIR /home/opuser

CMD ["/usr/local/bin/start-desktop.sh"]
