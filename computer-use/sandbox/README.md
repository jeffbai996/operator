# Operator sandbox desktop image

A real **isolated** Linux desktop for the Operator `desktop-sandbox` surface.
Runs its own Xvfb and a full XFCE4 desktop on `:1` inside a Docker container;
the host drives it via `docker exec` (an ffmpeg MJPEG pipe for the live feed,
`scrot` for screenshots, `xdotool` to inject input).
Isolation is real: own rootfs, network/PID namespace, non-root `opuser` — nothing
it does reaches the host WSL. Contrast the old fake "sandbox" (an Xvfb display on
the host, no isolation at all).

## Build

```bash
docker build -t operator-sandbox:latest .
```

## Lifecycle (managed by `../sandbox_container.py`)

- **Persistent.** Created on first sandbox use with `--restart unless-stopped`;
  its state and home volume survive leaving Operator, page reloads and
  host/docker restarts.
- **Paused when unused.** With no `docker exec` client attached and no use for
  `OPERATOR_SANDBOX_IDLE_STOP_S` seconds (default 900, `0` disables), the
  Operator worker stops the container. The next sandbox use starts it again.
- **Only an explicit delete destroys it** (`sandbox_container.delete()` / the
  UI's delete action → `docker rm -f operator-sandbox`). Switching surfaces or
  closing the page never tears it down.
- `ensure()` is idempotent: creates → starts → no-ops depending on state.

## Contents

debian-slim + Xvfb, XFCE4 (xfce4-terminal, thunar, mousepad, ristretto,
galculator), atril, xarchiver, LibreOffice Writer and Calc, **chromium**,
ffmpeg, xdotool, scrot. Add apps by extending the Dockerfile and rebuilding.

## Overrides (env)

- `OPERATOR_SANDBOX_CONTAINER` — container name (default `operator-sandbox`)
- `OPERATOR_SANDBOX_IMAGE` — image tag (default `operator-sandbox:latest`)
- `OPERATOR_SANDBOX_IDLE_STOP_S` — idle seconds before the container is paused
  (default `900`, `0` disables)
