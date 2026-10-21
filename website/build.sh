#!/usr/bin/env bash
# Build the complete static website before uploading website/public/.
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
builder=${1:-zensical}
case "$builder" in
  zensical) requirements="$root/docs/requirements.txt" ;;
  mkdocs) requirements="$root/docs/requirements-mkdocs.txt" ;;
  *) echo "Usage: $0 [zensical|mkdocs]" >&2; exit 2 ;;
esac

venv=$(mktemp -d)
trap 'rm -rf -- "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check -r "$requirements"
cd -- "$root"
"$venv/bin/$builder" build --strict
# The language switcher loads a sitemap relative to each language's root.
# Both languages share one build, so provide the matching subsets locally.
"$venv/bin/python" - <<'PY'
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

docs = Path("website/public/docs")
namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
ET.register_namespace("", namespace)
sitemap = ET.parse(docs / "sitemap.xml")
for language in ("en", "de"):
    subset = ET.Element(f"{{{namespace}}}urlset")
    for entry in sitemap.getroot():
        location = entry.find(f"{{{namespace}}}loc")
        if location is not None and urlsplit(location.text or "").path.startswith(
            f"/docs/{language}/"
        ):
            subset.append(entry)
    ET.ElementTree(subset).write(
        docs / language / "sitemap.xml", encoding="utf-8", xml_declaration=True
    )
PY
# Zensical currently copies these despite exclude_docs in mkdocs.yml.
rm -f -- website/public/docs/requirements.txt website/public/docs/requirements-mkdocs.txt
echo "Website built in $root/website/public"
