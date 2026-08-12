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

## Bundled React observer assets

The local observer page includes minified React and ReactDOM 18.3.1 browser
builds copied into the LLM Olympics wheel and source distribution. React is
copyright Meta Platforms, Inc. and affiliates and is licensed under the MIT
License. The complete upstream license text is distributed at
`llmolympic/web/static/REACT_LICENSE.txt`.

- Source: <https://github.com/facebook/react/tree/v18.3.1>
- Bundled files: `react.production.min.js`, `react-dom.production.min.js`

The observer's `app.js`, `app.css`, and `index.html` are original LLM Olympics
files covered by the project's MIT License.
