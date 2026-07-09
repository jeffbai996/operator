# Minimal isolated Linux desktop for the Operator sandbox surface.
# Runs its own Xvfb + openbox + a panel on :1; the host drives it via
# `docker exec` (scrot to capture, xdotool to inject input).
FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:1

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11-xserver-utils xdotool scrot \
      openbox tint2 \
      xfce4-terminal pcmanfm \
      chromium \
      fonts-dejavu-core \
      x11-utils \
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
