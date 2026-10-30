# Third-Party Notices

This file lists every third-party component that Lastro incorporates or distributes, as of
version 0.1.0. Versions match the ones pinned in `uv.lock`. Update this file whenever a
dependency or a bundled asset changes.

## Incorporated components

Components whose files are copied into this repository and shipped inside the Lastro package.

| Project | Author or organization | URL | Version | License | Modifications | Required notices |
|---|---|---|---|---|---|---|
| htmx | Big Sky Software | https://github.com/bigskysoftware/htmx | 2.0.11 | 0BSD | None. `src/lastro_demo/static/htmx.min.js` is byte-identical to `dist/htmx.min.js` of the 2.0.11 release (SHA-256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`). | None required by 0BSD; attribution kept in NOTICE. |

Lastro does not incorporate any adapted skill or third-party prompt at this version.

## Runtime dependencies

Python packages that installing Lastro pulls from PyPI. They are not copied into the Lastro
package; each one is installed from its own distribution, which carries its license text.

| Project | Author or organization | URL | Version | License | Modifications | Required notices |
|---|---|---|---|---|---|---|
| annotated-doc | Sebastián Ramírez | https://github.com/fastapi/annotated-doc | 0.0.5 | MIT | None | License text shipped with the package |
| annotated-types | Adrian Garcia Badaracco and contributors | https://github.com/annotated-types/annotated-types | 0.8.0 | MIT | None | License text shipped with the package |
| anyio | Alex Grönholm | https://github.com/agronholm/anyio | 4.15.1 | MIT | None | License text shipped with the package |
| certifi | Kenneth Reitz | https://github.com/certifi/python-certifi | 2026.10.23 | MPL-2.0 | None | Source available at the project URL, as MPL-2.0 requires |
| cffi | Armin Rigo, Maciej Fijalkowski | https://github.com/python-cffi/cffi | 2.1.1 | MIT-0 | None | License text shipped with the package |
| click | Pallets | https://github.com/pallets/click | 8.5.0 | BSD-3-Clause | None | License text shipped with the package |
| colorama (Windows only) | Jonathan Hartley | https://github.com/tartley/colorama | 0.4.6 | BSD-3-Clause | None | License text shipped with the package |
| cryptography | The Python Cryptographic Authority and contributors | https://github.com/pyca/cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | None | License texts shipped with the package |
| fastapi | Sebastián Ramírez | https://github.com/fastapi/fastapi | 0.162.0 | MIT | None | License text shipped with the package |
| greenlet | Alexey Borzenkov and contributors | https://github.com/python-greenlet/greenlet | 3.5.6 | MIT AND PSF-2.0 | None | License texts shipped with the package |
| h11 | Nathaniel J. Smith | https://github.com/python-hyper/h11 | 0.16.0 | MIT | None | License text shipped with the package |
| httpcore | Encode (Tom Christie) | https://github.com/encode/httpcore | 1.0.9 | BSD-3-Clause | None | License text shipped with the package |
| httpx | Encode (Tom Christie) | https://github.com/encode/httpx | 0.28.1 | BSD-3-Clause | None | License text shipped with the package |
| idna | Kim Davies | https://github.com/kjd/idna | 3.20 | BSD-3-Clause | None | License text shipped with the package |
| itsdangerous | Pallets | https://github.com/pallets/itsdangerous | 2.2.0 | BSD-3-Clause | None | License text shipped with the package |
| jinja2 | Pallets | https://github.com/pallets/jinja | 3.1.6 | BSD-3-Clause | None | License text shipped with the package |
| markdown-it-py | Chris Sewell and contributors | https://github.com/executablebooks/markdown-it-py | 4.2.0 | MIT | None | License text shipped with the package |
| markupsafe | Pallets | https://github.com/pallets/markupsafe | 3.0.4 | BSD-3-Clause | None | License text shipped with the package |
| mdurl | Taneli Hukkinen | https://github.com/executablebooks/mdurl | 0.1.2 | MIT | None | License text shipped with the package |
| opentelemetry-api | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 1.45.1 | Apache-2.0 | None | License and notices shipped with the package |
| playwright | Microsoft Corporation | https://github.com/microsoft/playwright-python | 1.64.1 | Apache-2.0 | None | License and notices shipped with the package |
| pycparser | Eli Bendersky | https://github.com/eliben/pycparser | 3.12 | BSD-3-Clause | None | License text shipped with the package |
| pydantic | Pydantic (Samuel Colvin and contributors) | https://github.com/pydantic/pydantic | 2.14.1 | MIT | None | License text shipped with the package |
| pydantic-core | Pydantic (Samuel Colvin and contributors) | https://github.com/pydantic/pydantic-core | 2.50.1 | MIT | None | License text shipped with the package |
| pyee | Josh Holbrook | https://github.com/jfhbrook/pyee | 14.0.0 | MIT | None | License text shipped with the package |
| pygments | Georg Brandl and contributors | https://github.com/pygments/pygments | 2.21.0 | BSD-2-Clause | None | License text shipped with the package |
| pyjwt | Jose Padilla | https://github.com/jpadilla/pyjwt | 2.15.1 | MIT | None | License text shipped with the package |
| python-dotenv | Saurabh Kumar | https://github.com/theskumar/python-dotenv | 1.2.4 | BSD-3-Clause | None | License text shipped with the package |
| python-multipart | Andrew Dunham and Marcelo Trylesinski | https://github.com/Kludex/python-multipart | 0.0.32 | Apache-2.0 | None | License and notices shipped with the package |
| rich | Will McGugan (Textualize) | https://github.com/Textualize/rich | 15.0.0 | MIT | None | License text shipped with the package |
| shellingham | Tzu-ping Chung | https://github.com/sarugaku/shellingham | 1.5.4 | ISC | None | License text shipped with the package |
| starlette | Encode (Tom Christie) and Marcelo Trylesinski | https://github.com/Kludex/starlette | 1.10.0 | BSD-3-Clause | None | License text shipped with the package |
| typer | Sebastián Ramírez | https://github.com/fastapi/typer | 0.27.3 | MIT | None | License text shipped with the package |
| typing-extensions | Python Software Foundation and contributors | https://github.com/python/typing_extensions | 4.16.0 | PSF-2.0 | None | License text shipped with the package |
| typing-inspection | Victorien Plot (Pydantic) | https://github.com/pydantic/typing-inspection | 0.4.4 | MIT | None | License text shipped with the package |
| tzdata | Python Software Foundation | https://github.com/python/tzdata | 2026.6 | Apache-2.0 | None | License and notices shipped with the package |
| uvicorn | Encode (Tom Christie) and Marcelo Trylesinski | https://github.com/Kludex/uvicorn | 0.54.0 | BSD-3-Clause | None | License text shipped with the package |

The Chromium build used by Playwright is downloaded separately with `playwright install
chromium` and is not distributed with Lastro.

Development tools (ruff, pyright, pytest, pytest-cov, pre-commit, gitleaks and the
pre-commit hooks) are used only to build and check Lastro and are not distributed with it.
