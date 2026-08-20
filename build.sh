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
  ./build.sh -si              Build/install all four split packages
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

# python-build and python-installer are installed only for /usr/bin/python
# the PKGBUILD derives the installed site-packages from whichever python it finds first,
# so systems that have python earlier on the path (such as via mise, in the case of Omarchy 4 machines)
# must explicityly use /usr/bin/python
if [[ ! -x /usr/bin/python ]]; then
    echo "error: /usr/bin/python not found; install the 'python' package" >&2
    exit 127
fi
shadowing_python=$(command -v python 2>/dev/null || true)
if [[ -n "$shadowing_python" && ! "$shadowing_python" -ef /usr/bin/python ]]; then
    echo "note: ignoring $shadowing_python; building with /usr/bin/python"
fi
export PATH="/usr/bin:$PATH"

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
snapshot_work=$(mktemp -d)
trap 'rm -f -- "$temporary"; rm -rf -- "$snapshot_work"' EXIT

mapfile -d '' candidates < <(
    git -C "$ROOT" ls-files --cached --others --exclude-standard -z
)
files=()
for file in "${candidates[@]}"; do
    # Deleted tracked files remain in the index until committed.
    # DEB/RPM-only private wheels must not enter the Arch source archive.
    if [[ "$file" == packaging/vendor/textual/* ]]; then
        continue
    fi
    if [[ -e "$ROOT/$file" || -L "$ROOT/$file" ]]; then
        files+=("$file")
    fi
done

if (( ${#files[@]} == 0 )); then
    echo "error: source snapshot would be empty" >&2
    exit 2
fi

source_epoch=$(git -C "$ROOT" log -1 --format=%ct)
snapshot_dir="$snapshot_work/blueferry-$version"
mkdir -p "$snapshot_dir"
tar -C "$ROOT" -cf - -- "${files[@]}" | tar -C "$snapshot_dir" -xf -
build_sha=$(
    tar -C "$snapshot_dir" \
        --sort=name \
        --mtime="@$source_epoch" \
        --owner=0 --group=0 --numeric-owner \
        -cf - . | sha256sum | cut -d' ' -f1
)
printf '%s\n' "$build_sha" > "$snapshot_dir/.blueferry-build-sha"

tar -C "$snapshot_work" \
    --sort=name \
    --mtime="@$source_epoch" \
    --owner=0 --group=0 --numeric-owner \
    -cf - "blueferry-$version" | gzip -n > "$temporary"
mv -f -- "$temporary" "$archive"
rm -rf -- "$snapshot_work"
trap - EXIT

# makepkg must verify exactly the snapshot it is about to compile. Keep this
# generated value outside the snapshot so the archive is reproducible.
digest=$(sha256sum "$archive" | cut -d' ' -f1)
printf '%s\n' "$digest" > "$ARCH_DIR/.source-sha256"

echo "Prepared $archive"
echo "SHA-256: $digest"
echo "Build SHA: $build_sha"
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
