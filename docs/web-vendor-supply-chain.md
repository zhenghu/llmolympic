# Web vendor supply chain

The observer is self-hosted and has no runtime Node.js or CDN dependency. Its single
same-origin JavaScript file is a production IIFE built from the reviewed source in
`web_src/app.js` and exact npm package versions. This keeps the browser CSP free of
third-party origins and runtime module loaders while supporting React 19, which no longer
publishes the UMD files used by the former copy-based process.

## Recorded source and build

The canonical provenance record is `scripts/web_vendor_manifest.json`; npm tarball
integrity is independently locked by `package-lock.json`.

| Build input | Version | Role | License |
| --- | --- | --- | --- |
| `react` | 19.2.8 | Bundled runtime | MIT |
| `react-dom` | 19.2.8 | Bundled runtime | MIT |
| `scheduler` | 0.27.0 | Bundled transitive runtime | MIT |
| `esbuild` | 0.28.2 | Build-only compiler | MIT |

The manifest records each package's exact version, official registry tarball URL, and npm
SHA-512 integrity value. It also fixes the complete production build contract: entry and
output paths, IIFE format, browser platform, ES2020 target, production environment,
minification, tree shaking, UTF-8 output, disabled source maps, and preservation of upstream
legal comments at the end of the bundle. The SHA-256 digests of every project-local build input
and the final bundle are
recorded as a second, review-friendly integrity layer.

React's complete MIT text is committed as
`llmolympic/web/static/REACT_LICENSE.txt` and is distributed next to the application assets.
The corresponding third-party attribution is in `THIRD_PARTY_NOTICES.md`.

## Reproduce and verify

Use a supported Node.js release, then run:

```bash
npm ci --ignore-scripts
npm run verify:web-vendor
npm audit --audit-level=high
```

`verify:web-vendor` does not trust the committed bundle. It requires all of the following:

1. every declared package matches the exact version, registry tarball, and integrity value in
   `package-lock.json`;
2. direct dependencies are exact pins in both `package.json` and the lockfile root, and every
   installed package has the declared version;
3. esbuild's dependency graph contains exactly the declared runtime packages and project-local
   source files, with no undeclared input;
4. the local source hashes and the fixed build configuration match the manifest;
5. a fresh in-memory production build has the declared output SHA-256 and is byte-for-byte
   identical to `llmolympic/web/static/assets/app.js`.

To deliberately regenerate the committed bundle after reviewing an input change, run:

```bash
npm run build:web
npm run verify:web-vendor
```

The verifier will fail after a legitimate source or dependency change until the reviewer also
updates the manifest hashes. CI runs the same reconstruction and comparison, so a hand-edited
or stale bundle cannot pass merely by changing its recorded digest.

## Upgrade procedure

1. Review the upstream releases, licenses, and security advisories for React, ReactDOM,
   Scheduler, and esbuild. Treat major React changes as application and CSP design changes.
2. Install exact direct versions, for example:

   ```bash
   npm install --save-dev --save-exact react@VERSION react-dom@VERSION esbuild@VERSION
   ```

3. Review `web_src/app.js` and update it for the new public APIs. Do not import code from an
   unversioned URL or add a runtime CDN dependency.
4. Update every affected package field and source hash in
   `scripts/web_vendor_manifest.json`, run `npm run build:web`, and record the resulting bundle
   SHA-256. Keep `scripts/build_web.mjs` and the manifest build contract identical.
5. Confirm `REACT_LICENSE.txt` and `THIRD_PARTY_NOTICES.md` remain accurate.
6. Run `npm run verify:web-vendor`, the browser unit and E2E suites, Python tests,
   distribution verification, and dependency audits before review.

Do not weaken verification to accept only a committed digest: provenance requires the locked
npm tarballs, a closed input graph, a fixed production build, and an exact reconstruction.
