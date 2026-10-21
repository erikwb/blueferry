# BlueFerry website

The existing Cloudflare Worker `snowy-brook-1958` serves static files at
<https://blueferry.weirdware.io/>. It was originally uploaded through the
Cloudflare dashboard. It has no application code or bindings. Keep its name
when uploading updates so the existing custom domain continues to work.

`public/` contains the landing page and assets. The documentation build adds
`public/docs/`; upload the complete `public/` directory together. Generated
documentation stays out of Git.

## Build and check locally

From the repository root, with Python 3 and its `venv` module installed:

```sh
./website/build.sh
```

This installs the pinned Zensical requirements in a temporary environment,
runs a strict build, and writes the per-language sitemaps used by the language
switcher. To check the supported MkDocs fallback too:

```sh
./website/build.sh mkdocs
```

Wrangler runs the default Zensical build before each upload, so a failed build
stops deployment. These checks run locally without GitHub Actions credits.

## Preview and publish

Wrangler needs Node.js and an account with access to the existing Worker.
Authenticate once, then upload a version without changing the live site:

```sh
npx wrangler@4 login
npx wrangler@4 versions upload --config website/wrangler.toml --preview-alias docs-preview
```

Open the version URL printed by Wrangler. Check the landing-page links,
`/docs/`, English and German navigation, search, and a page with a Mermaid
diagram. The `_headers` file allows the scripts and search requests needed
under `/docs/` while keeping the landing page's stricter policy.

Promote the tested version, replacing `VERSION_ID` with its UUID:

```sh
npx wrangler@4 versions deploy VERSION_ID@100% --config website/wrangler.toml
```

This publishes the already uploaded assets without rebuilding them. Check
<https://blueferry.weirdware.io/docs/> after deployment.

To inspect deployments or roll back to the previous version:

```sh
npx wrangler@4 deployments list --config website/wrangler.toml
npx wrangler@4 rollback --config website/wrangler.toml
```

The site uses Workers Static Assets. Do not use `wrangler pages deploy` or
create a new Pages project for it. See Cloudflare's documentation for
[version URLs](https://developers.cloudflare.com/workers/versions-and-deployments/version-urls/)
and [deployments](https://developers.cloudflare.com/workers/versions-and-deployments/).
