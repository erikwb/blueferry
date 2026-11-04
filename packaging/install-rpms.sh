#!/usr/bin/env bash
# Install the BlueFerry RPMs from one directory in a Fedora CI container.
# Fedora 43 has no quickshell package, so blueferry-quickshell is installed
# and checked only where the release provides quickshell (Fedora 44 and newer).
set -euo pipefail

dir=${1:?usage: install-rpms.sh DIR}
shopt -s nullglob
packages=()
quickshell=()
for rpm in "$dir"/*.noarch.rpm; do
    case ${rpm##*/} in
        blueferry-quickshell-*) quickshell+=("$rpm") ;;
        *) packages+=("$rpm") ;;
    esac
done
if (( ${#quickshell[@]} )) && dnf -q repoquery quickshell | grep -q .; then
    packages+=("${quickshell[@]}")
fi
dnf install -y --allowerasing "${packages[@]}"

if rpm -q blueferry-quickshell >/dev/null 2>&1; then
    test -x /usr/bin/blueferry-quickshell
    test -f /usr/share/blueferry/quickshell/shell.qml
    # The packaged ConversationLogic is the shared implementation, not the
    # development wrapper that imports from the source tree.
    if grep -F '../../src/' /usr/share/blueferry/quickshell/ConversationLogic.qml; then
        echo "blueferry-quickshell ships the development wrapper" >&2
        exit 1
    fi
    echo "blueferry-quickshell installed"
else
    echo "quickshell is not available on this release; skipped blueferry-quickshell"
fi
