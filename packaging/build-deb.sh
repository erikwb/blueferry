#!/usr/bin/env bash
# Build native Debian-family packages from a clean snapshot of this worktree.

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT="$ROOT/dist/deb"
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT

for command in dpkg-buildpackage git python3 sha256sum tar; do
    command -v "$command" >/dev/null || {
        echo "error: required command not found: $command" >&2
        exit 127
    }
done

version=$(python3 - "$ROOT/pyproject.toml" <<'PY'
import pathlib
import sys
import tomllib

with pathlib.Path(sys.argv[1]).open("rb") as stream:
    print(tomllib.load(stream)["project"]["version"])
PY
)
debian_version=$(dpkg-parsechangelog -l"$ROOT/packaging/deb/changelog" -S Version)
if [[ "$debian_version" != "$version-1" ]]; then
    echo "error: pyproject version $version != Debian version $debian_version" >&2
    exit 2
fi

source_dir="$WORK/blueferry-$version"
mkdir -p "$source_dir"
mapfile -d '' candidates < <(
    git -C "$ROOT" ls-files --cached --others --exclude-standard -z
)
files=()
for file in "${candidates[@]}"; do
    if [[ -e "$ROOT/$file" || -L "$ROOT/$file" ]]; then
        files+=("$file")
    fi
done
if (( ${#files[@]} == 0 )); then
    echo "error: source snapshot would be empty" >&2
    exit 2
fi

tar -C "$ROOT" -cf - -- "${files[@]}" | tar -C "$source_dir" -xf -
source_epoch=$(git -C "$ROOT" log -1 --format=%ct)
build_sha=$(
    tar -C "$source_dir" --sort=name --mtime="@$source_epoch" \
        --owner=0 --group=0 --numeric-owner -cf - . \
        | sha256sum | cut -d' ' -f1
)
printf '%s\n' "$build_sha" > "$source_dir/.blueferry-build-sha"
mkdir "$source_dir/debian"
cp -a "$source_dir/packaging/deb/." "$source_dir/debian/"

tar -C "$WORK" --sort=name --mtime="@$source_epoch" \
    --owner=0 --group=0 --numeric-owner \
    --exclude="blueferry-$version/debian" \
    -czf "$WORK/blueferry_${version}.orig.tar.gz" "blueferry-$version"

(cd "$source_dir" && dpkg-buildpackage --no-sign -b)
mkdir -p "$OUTPUT"
find "$WORK" -maxdepth 1 -type f \
    \( -name '*.deb' -o -name '*.buildinfo' -o -name '*.changes' \) \
    -exec cp -f -- {} "$OUTPUT/" \;
echo "Packages written to $OUTPUT"
