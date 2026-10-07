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
[MkDocs](https://www.mkdocs.org/) and the
[Material theme](https://squidfunk.github.io/mkdocs-material/). The
configuration in `mkdocs.yml` uses no third-party plugins, so it should also
build with [Zensical](https://zensical.org/), Material's successor.

```sh
python -m venv /tmp/blueferry-docs
/tmp/blueferry-docs/bin/pip install -r docs/requirements.txt
/tmp/blueferry-docs/bin/mkdocs build --strict
```

The build writes to `website/public/docs/`, which Git ignores, so the landing
page links to `/docs/` on the published site. `mkdocs serve` previews it at
`http://127.0.0.1:8000/docs/`.

### Cloudflare Pages

| Setting | Value |
| --- | --- |
| Build command | `pip install -r docs/requirements.txt && mkdocs build --strict` |
| Build output directory | `website/public` |
| Root directory | *(repository root)* |
| Environment variable | `PYTHON_VERSION` = `3.13` |

Without a build command, Cloudflare Pages keeps serving the landing page
alone; only the `Docs` link would then return 404.

The `docs` GitHub workflow runs the same `mkdocs build --strict` when files
under `docs/` or `mkdocs.yml` change. It is separate from the quality and
package workflows.
