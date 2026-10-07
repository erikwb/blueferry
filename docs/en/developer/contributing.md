# Contributing

Issues and pull requests are welcome on
[GitHub](https://github.com/erikwb/blueferry).

## Before you start

- Read [ARCHITECTURE.md](https://github.com/erikwb/blueferry/blob/main/ARCHITECTURE.md),
  [PROTOCOL.md](https://github.com/erikwb/blueferry/blob/main/PROTOCOL.md), and
  [TESTING.md](https://github.com/erikwb/blueferry/blob/main/TESTING.md).
- BlueFerry focuses on messaging. For larger features, consider opening an
  issue first.

## Quality gates

Every pull request runs the quality workflow on Arch Linux: Ruff, Bandit,
mypy, the hermetic test suite with coverage, and QML linting. A separate
workflow builds and installs the native packages. Run the same checks
locally; see [Testing](testing.md).

## Privacy rules for code

- `Events1` signals stay content-free. Clients fetch details through
  authenticated, rate-limited `Messages1` methods.
- Logs never contain message text, phone numbers, names, or notification
  content. Log lengths, IDs, or states instead.
- Never block the GLib main loop. Use asynchronous D-Bus calls or the OBEX
  worker.

## Conventions

- Commit subjects are short and imperative, without a trailing period, with
  an explanatory body.
- User-visible Python strings go through gettext, QML strings through
  `qsTr`. Add new source files to `po/POTFILES.in`.
- When you change the D-Bus API, update
  `data/io.weirdware.BlueFerry.xml` and the client models in the same change.

## Documentation

- Update `README.md`, `ARCHITECTURE.md`, and `PROTOCOL.md` where your change
  affects them.
- A user-facing feature adds or extends a page under `docs/en/`. A German
  translation under `docs/de/` is welcome but not required; the German site
  links to English where a translation is missing.
- Pages are plain Markdown and must read well on GitHub. Use relative links
  between pages and full GitHub links to files outside `docs/`.
- Check the site with `zensical build --strict`; see
  [docs/README.md](../../README.md).
