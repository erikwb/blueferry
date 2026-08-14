#!/usr/bin/env bash
# Build native Fedora RPMs from a deterministic snapshot of this worktree.

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT="$ROOT/dist/rpm"
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT

for command in git gzip python3 rpmbuild sha256sum tar; do
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
spec_version=$(sed -n 's/^Version:[[:space:]]*//p' "$ROOT/packaging/rpm/blueferry.spec")
if [[ "$version" != "$spec_version" ]]; then
    echo "error: pyproject version $version != RPM version $spec_version" >&2
    exit 2
fi

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

topdir="$WORK/rpmbuild"
mkdir -p "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
archive="$topdir/SOURCES/blueferry-$version.tar.gz"
temporary="$archive.tmp"
source_epoch=$(git -C "$ROOT" log -1 --format=%ct)
source_dir="$WORK/blueferry-$version"
mkdir -p "$source_dir"
tar -C "$ROOT" -cf - -- "${files[@]}" | tar -C "$source_dir" -xf -
build_sha=$(
    tar -C "$source_dir" --sort=name --mtime="@$source_epoch" \
        --owner=0 --group=0 --numeric-owner -cf - . \
        | sha256sum | cut -d' ' -f1
)
printf '%s\n' "$build_sha" > "$source_dir/.blueferry-build-sha"
tar -C "$WORK" --sort=name --mtime="@$source_epoch" \
    --owner=0 --group=0 --numeric-owner \
    -cf - "blueferry-$version" | gzip -n > "$temporary"
mv -f -- "$temporary" "$archive"
cp "$ROOT/packaging/rpm/blueferry.spec" "$topdir/SPECS/"

rpmbuild -ba --define "_topdir $topdir" "$topdir/SPECS/blueferry.spec"
mkdir -p "$OUTPUT"
find "$topdir/RPMS" "$topdir/SRPMS" -type f -name '*.rpm' \
    -exec cp -f -- {} "$OUTPUT/" \;
echo "Packages written to $OUTPUT"
