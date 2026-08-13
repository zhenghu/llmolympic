# Third-party notices

The MIT License in [LICENSE](LICENSE) and the `License-Expression: MIT` package
metadata apply to the original files contained in the LLM Olympics distribution
archives, unless a file states otherwise. External dependencies retain their own
licenses and are not relicensed by this project.

## python-chess

LLM Olympics declares the separately installed Python package `chess>=1.11.2,<2`
for its international-chess game. The upstream python-chess project licenses that
package under GPL-3.0-or-later:

- Source: <https://github.com/niklasf/python-chess>
- Documentation: <https://python-chess.readthedocs.io/>

The python-chess package is not copied into the LLM Olympics wheel or source
distribution. When using or redistributing an installed environment or bundle that
combines LLM Olympics with python-chess, comply with the applicable licenses,
including the GPL terms for python-chess. This notice is informational and is not
legal advice.

## Optional Web dependencies

The optional `web` installation extra installs FastAPI (MIT), Uvicorn
(BSD-3-Clause), and websockets (BSD-3-Clause), together with their separately
distributed dependencies. These
packages are not copied into the LLM Olympics wheel or source distribution and retain
their upstream licenses:

- FastAPI: <https://github.com/fastapi/fastapi>
- Uvicorn: <https://github.com/Kludex/uvicorn>
- websockets: <https://github.com/python-websockets/websockets>

## Bundled React observer code

The local observer's production `app.js` bundle includes React 19.2.8,
ReactDOM 19.2.8, and Scheduler 0.27.0. These projects are copyright Meta
Platforms, Inc. and affiliates and are licensed under the MIT License. The
complete upstream license text is distributed at
`llmolympic/web/static/REACT_LICENSE.txt`.
The production bundle also carries React and Modernizr MIT notices; retained
upstream React comments and a fixed banner keep those notices with the served copy.

- Source: <https://github.com/facebook/react/tree/v19.2.8>
- Bundled file: `llmolympic/web/static/assets/app.js`
- Reproducible source manifest: `scripts/web_vendor_manifest.json`
- Verification procedure: `docs/web-vendor-supply-chain.md`

The bundle is produced with esbuild 0.28.2, an MIT-licensed build tool. esbuild
itself is not copied into the LLM Olympics wheel or source distribution.

- Source: <https://github.com/evanw/esbuild/tree/v0.28.2>

The observer source in `web_src/app.js`, its CSS, and its HTML are original LLM
Olympics files covered by the project's MIT License; the generated `app.js`
contains both that original application code and the bundled third-party code
listed above.
