# BlueFerry documentation

BlueFerry brings SMS, RCS, and iMessage from a paired iPhone to the Linux
desktop over Bluetooth.

- **[English documentation](en/index.md)**
- **[Deutsche Dokumentation](de/index.md)**

![BlueFerry's Quickshell client with sample conversations](images/quickshell.png)

## About these pages

Every page is plain Markdown and reads fine on GitHub without any tooling.
Links between pages are ordinary relative links. Files outside `docs/`, such as
[ARCHITECTURE.md](https://github.com/erikwb/blueferry/blob/main/ARCHITECTURE.md),
are linked on GitHub instead of copied, so they keep a single source.

The pages are organized by language:

| Folder | Content |
| --- | --- |
| `en/` | English user and developer guide |
| `de/` | German user guide and developer overview; other developer pages link to English |
| `images/` | Screenshots and logo, copied from `website/public/assets/` |

### Optional website build

The project website in `website/public/` needs no build step. The
documentation site is an optional addition built with
[Zensical](https://zensical.org/), the successor of Material for MkDocs. It
reads the `mkdocs.yml` in the repository root.

```sh
python -m venv /tmp/blueferry-docs
/tmp/blueferry-docs/bin/pip install -r docs/requirements.txt
/tmp/blueferry-docs/bin/zensical build --strict
```

The build writes to `website/public/docs/`, which Git ignores. `zensical serve`
previews it at `http://localhost:8000/docs/`. `--strict` fails the build on
broken links, anchors, or images.

The configuration uses no third-party plugins, so it also builds with
[MkDocs](https://www.mkdocs.org/) and the
[Material theme](https://squidfunk.github.io/mkdocs-material/):

```sh
/tmp/blueferry-docs/bin/pip install -r docs/requirements-mkdocs.txt
/tmp/blueferry-docs/bin/mkdocs build --strict
```

Diagrams are Mermaid code blocks. GitHub renders them natively, and the site
build loads Mermaid in the browser.

### Publishing

Nothing builds or publishes these pages yet: no workflow runs the build, and
the landing page does not link to `/docs/`. The configuration assumes the site
is served at `https://blueferry.weirdware.io/docs/`; the language switcher and
some German navigation entries use absolute `/docs/...` paths.

Publishing it there needs two changes outside this folder. The host has to run
the build command above before it serves `website/public/`, and
`website/public/_headers` needs a separate Content-Security-Policy for
`/docs/*`. The current policy allows scripts and styles from the site itself
only, while the documentation theme uses inline scripts and loads Mermaid from
`unpkg.com`.
