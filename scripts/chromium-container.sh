#!/bin/sh
# Chromium launcher used by Vivliostyle (via CHROMIUM_PATH) inside the container.
#
# Puppeteer invokes the browser binary directly, so the only way to force
# container-safe flags on every launch is to put them in front of its own
# arguments. `exec` replaces this shell with Chromium and preserves the pipe
# file descriptors Puppeteer uses to speak CDP, so nothing is lost by the hop.
#
#   --disable-dev-shm-usage  Chromium puts large shared buffers in /dev/shm,
#       which is 64 MB in most container runtimes (Railway included, where it
#       is not configurable). Without this flag, rendering a big or
#       fixed-layout book crashes the renderer with an opaque error. The flag
#       moves those buffers to /tmp instead.
#   --no-sandbox  the container has no user-namespace sandbox available and
#       runs as root. Vivliostyle passes this by default too; setting it here
#       keeps rendering working if that default ever changes.
exec /usr/bin/chromium \
    --disable-dev-shm-usage \
    --no-sandbox \
    "$@"
