#!/bin/sh
# Exercise the installed plugin manager's prerequisites without a network or
# desktop session. Run against the system Python used by native packages.
set -eu

git --version
plugin_env=$(mktemp -d)
trap 'rm -rf -- "$plugin_env"' EXIT HUP INT TERM
/usr/bin/python3 -m venv --system-site-packages "$plugin_env"
"$plugin_env/bin/python" -m pip --version
