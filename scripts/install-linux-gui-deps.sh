#!/usr/bin/env bash
# Install the Linux system libraries Qt/napari need for a real GL canvas.
#
# The "viewer" Python extra installs PySide6 and napari, but those wheels
# still link against system libraries that minimal container images omit.
# Without them you get, in order of discovery:
#
#   ImportError: libEGL.so.1: cannot open shared object file
#   qt.qpa.plugin: Could not load the Qt platform plugin "xcb"
#
# QT_QPA_PLATFORM=offscreen is NOT a workaround: it provides no
# QOpenGLWidget, so napari's GPU canvas is never exercised and its layer
# teardown raises KeyError. Use a virtual display instead (xvfb-run).
#
# Idempotent: safe to re-run. Debian/Ubuntu only; a no-op elsewhere.

set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
    echo "install-linux-gui-deps: not a Debian/Ubuntu system, skipping."
    exit 0
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "install-linux-gui-deps: need root or sudo to install packages." >&2
        exit 1
    fi
fi

PACKAGES=(
    # Virtual display, so the tests can run without a physical screen.
    xvfb
    # OpenGL / EGL: what PySide6.QtGui links against.
    libegl1
    libgl1
    libglx-mesa0
    # Software rasterizer, so GL works without a GPU (llvmpipe).
    libgl1-mesa-dri
    # Qt "xcb" platform plugin dependencies.
    libxcb-cursor0
    libxcb-icccm4
    libxcb-image0
    libxcb-keysyms1
    libxcb-randr0
    libxcb-render-util0
    libxcb-shape0
    libxcb-xinerama0
    libxkbcommon0
    libxkbcommon-x11-0
    # Misc Qt runtime dependencies.
    libdbus-1-3
    libfontconfig1
)

echo "install-linux-gui-deps: updating package lists..."
# Third-party PPAs may be unreachable behind a proxy; the main archive is
# what matters here, so a partial update is acceptable.
$SUDO apt-get update -qq || echo "  (some package lists failed; continuing)"

echo "install-linux-gui-deps: installing ${#PACKAGES[@]} packages..."
$SUDO apt-get install -y --no-install-recommends "${PACKAGES[@]}"

echo "install-linux-gui-deps: done."
