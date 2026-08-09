#!/usr/bin/env bash
# Build Arch split packages from the current working tree, including uncommitted
# hardening changes. Nothing is installed unless makepkg is given -i.

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ARCH_DIR="$ROOT/packaging/arch"

usage() {
    cat <<'EOF'
Usage:
  ./build.sh                  Prepare the snapshot and run makepkg -C -f -s
  ./build.sh -si              Build/install backend + GTK + Qt + Quickshell
  ./build.sh --prepare-only   Only refresh packaging/arch/blueferry-*.tar.gz
  ./build.sh -- <args...>     Pass arbitrary arguments to makepkg

The snapshot contains tracked and untracked, non-ignored files from the
current working tree. Build artifacts, .git, virtualenvs, caches, and private
runtime data are excluded by .gitignore.
EOF
}

prepare_only=false
if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    usage
    exit 0
elif [[ ${1:-} == "--prepare-only" ]]; then
    prepare_only=true
    shift
elif [[ ${1:-} == "--" ]]; then
    shift
fi

for command in git gzip makepkg python tar; do
    command -v "$command" >/dev/null || {
        echo "error: required command not found: $command" >&2
        exit 127
    }
done

version=$(python - "$ROOT/pyproject.toml" <<'PY'
import pathlib
import sys
import tomllib

with pathlib.Path(sys.argv[1]).open("rb") as stream:
    print(tomllib.load(stream)["project"]["version"])
PY
)

pkgbuild_version=$(sed -n 's/^pkgver=//p' "$ARCH_DIR/PKGBUILD")
if [[ "$version" != "$pkgbuild_version" ]]; then
    echo "error: pyproject version $version != PKGBUILD version $pkgbuild_version" >&2
    exit 2
fi

archive="$ARCH_DIR/blueferry-$version.tar.gz"
temporary="$archive.tmp.$$"
trap 'rm -f -- "$temporary"' EXIT

mapfile -d '' candidates < <(
    git -C "$ROOT" ls-files --cached --others --exclude-standard -z
)
files=()
for file in "${candidates[@]}"; do
    # Deleted tracked files remain in the index until committed.
    if [[ -e "$ROOT/$file" || -L "$ROOT/$file" ]]; then
        files+=("$file")
    fi
done

if (( ${#files[@]} == 0 )); then
    echo "error: source snapshot would be empty" >&2
    exit 2
fi

source_epoch=$(git -C "$ROOT" log -1 --format=%ct)
tar -C "$ROOT" \
    --sort=name \
    --mtime="@$source_epoch" \
    --owner=0 --group=0 --numeric-owner \
    --transform="s|^|blueferry-$version/|" \
    -cf - -- "${files[@]}" | gzip -n > "$temporary"
mv -f -- "$temporary" "$archive"
trap - EXIT

# makepkg must verify exactly the snapshot it is about to compile. Keep this
# generated value outside the snapshot so the archive is reproducible.
digest=$(sha256sum "$archive" | cut -d' ' -f1)
printf '%s\n' "$digest" > "$ARCH_DIR/.source-sha256"

echo "Prepared $archive"
echo "SHA-256: $digest"
echo "Snapshot files: ${#files[@]}"

if "$prepare_only"; then
    exit 0
fi

if (( $# == 0 )); then
    set -- -s
fi

cd "$ARCH_DIR"
# makepkg normally reuses src/. That can leave deleted files from an older
# working-tree snapshot in a later build, making check() test code that is no
# longer in the source archive. Always extract into a clean source tree.
exec makepkg --cleanbuild --force "$@"
