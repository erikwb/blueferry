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

The build writes to `website/public/docs/`, which Git ignores, so the landing
page links to `/docs/` on the published site. `zensical serve` previews it at
`http://localhost:8000/docs/`. `--strict` fails the build on broken links,
anchors, or images.

The configuration uses no third-party plugins, so it also builds with
[MkDocs](https://www.mkdocs.org/) and the
[Material theme](https://squidfunk.github.io/mkdocs-material/):

```sh
/tmp/blueferry-docs/bin/pip install -r docs/requirements-mkdocs.txt
/tmp/blueferry-docs/bin/mkdocs build --strict
```

Diagrams are Mermaid code blocks. GitHub renders them natively, and the site
build loads Mermaid in the browser.

### Cloudflare Pages

| Setting | Value |
| --- | --- |
| Build command | `pip install -r docs/requirements.txt && zensical build --strict` |
| Build output directory | `website/public` |
| Root directory | *(repository root)* |
| Environment variable | `PYTHON_VERSION` = `3.13` |

Without a build command, Cloudflare Pages keeps serving the landing page
alone; only the `Docs` link would then return 404. `website/public/_headers`
gives `/docs/*` its own Content-Security-Policy, because the documentation
theme needs inline scripts and loads Mermaid from `unpkg.com`.

The `Docs` GitHub workflow builds the site with Zensical and with MkDocs when
files under `docs/` or `mkdocs.yml` change. It is separate from the quality
and package workflows.
