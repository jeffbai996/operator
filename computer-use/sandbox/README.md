# Operator sandbox desktop image

A real **isolated** Linux desktop for the Operator `desktop-sandbox` surface.
Runs its own Xvfb + openbox + panel on `:1` inside a Docker container; the host
drives it via `docker exec` (`scrot` to capture, `xdotool` to inject input).
Isolation is real: own rootfs, network/PID namespace, non-root `opuser` — nothing
it does reaches the host WSL. Contrast the old fake "sandbox" (an Xvfb display on
the host, no isolation at all).

## Build

```bash
docker build -t operator-sandbox:latest .
```

## Lifecycle (managed by `../sandbox_container.py`)

- **Persistent.** Created on first sandbox use with `--restart unless-stopped`;
  survives leaving Operator, page reloads, host/docker restarts, and idle.
- **Only an explicit delete destroys it** (`sandbox_container.delete()` / the
  UI's delete action → `docker rm -f operator-sandbox`). Switching surfaces or
  closing the page never tears it down.
- `ensure()` is idempotent: creates → starts → no-ops depending on state.

## Contents

debian-slim + Xvfb, openbox, tint2, xfce4-terminal, pcmanfm, **chromium**,
xdotool, scrot. Add apps by extending the Dockerfile and rebuilding.

## Overrides (env)

- `OPERATOR_SANDBOX_CONTAINER` — container name (default `operator-sandbox`)
- `OPERATOR_SANDBOX_IMAGE` — image tag (default `operator-sandbox:latest`)
