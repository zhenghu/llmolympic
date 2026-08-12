# Web vendor supply chain

The observer is self-hosted and has no runtime Node.js or CDN dependency. Node.js and npm are
used only for browser tests and for verifying that the committed React browser files are exact
copies of official npm package contents.

## Recorded source

The canonical metadata is `scripts/web_vendor_manifest.json`; dependency tarball integrity is
also locked by `package-lock.json`.

| Bundled asset | Package source | Version | License |
| --- | --- | --- | --- |
| `react.production.min.js` | `react` from the npm registry | 18.3.1 | MIT |
| `react-dom.production.min.js` | `react-dom` from the npm registry | 18.3.1 | MIT |

The upstream MIT text is committed as `llmolympic/web/static/REACT_LICENSE.txt`. The manifest
records the registry tarball URL, npm SHA-512 integrity value, package-relative source path, and
SHA-256 digest for every distributed file.

## Reproduce and verify

Use a supported Node.js release, then run:

```bash
npm ci
npm run verify:web-vendor
npm audit --audit-level=high
```

The verifier requires all of the following to match before it succeeds:

1. the exact package version, registry tarball, and integrity value in `package-lock.json`;
2. the installed npm package version;
3. the SHA-256 digest of both the npm package file and the distributed file;
4. byte-for-byte equality between those two files.

CI executes these checks. The npm Dependabot entry supplies weekly update and advisory signals
for both the development tools and the React packages that anchor the bundled bytes.

## Upgrade procedure

1. Review the upstream release and security advisories. React 19 does not publish the same UMD
   layout, so a major upgrade requires an explicit build and CSP design review.
2. Install exact versions, for example
   `npm install --save-dev --save-exact react@VERSION react-dom@VERSION`.
3. Copy only the intended production browser files from each package into
   `llmolympic/web/static/assets/`.
4. Update every field in `scripts/web_vendor_manifest.json`, including SHA-256 values, and verify
   that `REACT_LICENSE.txt` and `THIRD_PARTY_NOTICES.md` remain accurate.
5. Run `npm run verify:web-vendor`, the browser E2E suite, Python tests, distribution verification,
   and the dependency audits before review.

Do not replace the files from an unversioned CDN URL or weaken the verifier to accept a digest
without also matching the locked npm package bytes.
