# Third-Party Notices

This file lists every third-party component that Lastro incorporates or distributes, as of
version 0.1.0. Versions match the ones pinned in `uv.lock`. Update this file whenever a
dependency or a bundled asset changes.

## Incorporated components

Components whose files are copied into this repository and shipped inside the Lastro package.

| Project | Author or organization | URL | Version | License | Modifications | Required notices |
|---|---|---|---|---|---|---|
| htmx | Big Sky Software | https://github.com/bigskysoftware/htmx | 2.0.11 | 0BSD | None. `src/lastro_demo/static/htmx.min.js` is byte-identical to `dist/htmx.min.js` of the 2.0.11 release (SHA-256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`). | None required by 0BSD; attribution kept in NOTICE. |
| Langfuse Docker Compose | Langfuse GmbH | https://github.com/langfuse/langfuse | commit 2365023d8542afc9b0d4e720a3bddc9ce301f342 | MIT | Adapted in `src/lastro/observability/resources/compose.yaml`: images pinned by version and digest, required secrets without example defaults, only the web UI published on 127.0.0.1, AI gateway removed, internal network, log rotation and telemetry disabled. Provenance in `src/lastro/observability/resources/README.md`. | MIT license notice of the upstream repository. |

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
| faker | Daniele Faraglia and contributors | https://github.com/joke2k/faker | 40.44.0 | MIT | None | License text shipped with the package |
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
| opentelemetry-api | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 1.45.2 | Apache-2.0 | None | License and notices shipped with the package |
| playwright | Microsoft Corporation | https://github.com/microsoft/playwright-python | 1.64.1 | Apache-2.0 | None | License and notices shipped with the package |
| pycparser | Eli Bendersky | https://github.com/eliben/pycparser | 3.12 | BSD-3-Clause | None | License text shipped with the package |
| pydantic | Pydantic (Samuel Colvin and contributors) | https://github.com/pydantic/pydantic | 2.14.1 | MIT | None | License text shipped with the package |
| pydantic-core | Pydantic (Samuel Colvin and contributors) | https://github.com/pydantic/pydantic-core | 2.50.1 | MIT | None | License text shipped with the package |
| pyee | Josh Holbrook | https://github.com/jfhbrook/pyee | 14.0.0 | MIT | None | License text shipped with the package |
| pygments | Georg Brandl and contributors | https://github.com/pygments/pygments | 2.21.0 | BSD-2-Clause | None | License text shipped with the package |
| pyjwt | Jose Padilla | https://github.com/jpadilla/pyjwt | 2.15.1 | MIT | None | License text shipped with the package |
| python-dotenv | Saurabh Kumar | https://github.com/theskumar/python-dotenv | 1.2.4 | BSD-3-Clause | None | License text shipped with the package |
| python-multipart | Andrew Dunham and Marcelo Trylesinski | https://github.com/Kludex/python-multipart | 0.0.32 | Apache-2.0 | None | License and notices shipped with the package |
| rich | Will McGugan (Textualize) | https://github.com/Textualize/rich | 14.3.4 | MIT | None | License text shipped with the package |
| shellingham | Tzu-ping Chung | https://github.com/sarugaku/shellingham | 1.5.4 | ISC | None | License text shipped with the package |
| starlette | Encode (Tom Christie) and Marcelo Trylesinski | https://github.com/Kludex/starlette | 1.10.0 | BSD-3-Clause | None | License text shipped with the package |
| typer | Sebastián Ramírez | https://github.com/fastapi/typer | 0.27.3 | MIT | None | License text shipped with the package |
| typing-extensions | Python Software Foundation and contributors | https://github.com/python/typing_extensions | 4.16.0 | PSF-2.0 | None | License text shipped with the package |
| typing-inspection | Victorien Plot (Pydantic) | https://github.com/pydantic/typing-inspection | 0.4.4 | MIT | None | License text shipped with the package |
| tzdata | Python Software Foundation | https://github.com/python/tzdata | 2026.6 | Apache-2.0 | None | License and notices shipped with the package |
| uvicorn | Encode (Tom Christie) and Marcelo Trylesinski | https://github.com/Kludex/uvicorn | 0.54.0 | BSD-3-Clause | None | License text shipped with the package |

## Optional observability dependencies

Installed only with the `obs` extra (`uv sync --extra obs` or `pip install 'lastro[obs]'`).
The base installation does not pull them.

| Project | Author or organization | URL | Version | License | Modifications | Required notices |
|---|---|---|---|---|---|---|
| opentelemetry-sdk | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 1.45.2 | Apache-2.0 | None | License and notices shipped with the package |
| opentelemetry-exporter-otlp-proto-http | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 1.45.2 | Apache-2.0 | None | License and notices shipped with the package |
| opentelemetry-exporter-otlp-proto-common | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 1.45.2 | Apache-2.0 | None | License and notices shipped with the package |
| opentelemetry-proto | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 1.45.2 | Apache-2.0 | None | License and notices shipped with the package |
| opentelemetry-semantic-conventions | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 0.66b2 | Apache-2.0 | None | License and notices shipped with the package |
| opentelemetry-exporter-otlp-common | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 0.66b2 | Apache-2.0 | None | License and notices shipped with the package |
| opentelemetry-exporter-http-transport | OpenTelemetry Authors | https://github.com/open-telemetry/opentelemetry-python | 0.66b2 | Apache-2.0 | None | License and notices shipped with the package |
| protobuf | Google LLC | https://github.com/protocolbuffers/protobuf | 7.36.2 | BSD-3-Clause | None | License text shipped with the package |
| googleapis-common-protos | Google LLC | https://github.com/googleapis/google-cloud-python | 1.76.0 | Apache-2.0 | None | License and notices shipped with the package |
| requests | Kenneth Reitz and contributors | https://github.com/psf/requests | 2.34.2 | Apache-2.0 | None | License and notices shipped with the package |
| urllib3 | Andrey Petrov and contributors | https://github.com/urllib3/urllib3 | 2.8.0 | MIT | None | License text shipped with the package |
| charset-normalizer | Ahmed TAHRI | https://github.com/jawah/charset_normalizer | 3.5.2 | MIT | None | License text shipped with the package |

The container images started by `lastro obs up` (Langfuse, ClickHouse, PostgreSQL, Redis and
MinIO) are downloaded by Docker from their own registries and are not distributed with Lastro.

The Chromium build used by Playwright is downloaded separately with `playwright install
chromium` and is not distributed with Lastro.

Development tools (ruff, pyright, pytest, pytest-cov, pre-commit, gitleaks and the
pre-commit hooks) are used only to build and check Lastro and are not distributed with it.

## Optional documentation sources dependencies

Installed only with the `sources` extra (`uv sync --extra sources` or
`pip install 'lastro[sources]'`). The base installation does not pull them.
charset-normalizer, protobuf, requests and urllib3 also appear with the observability extra,
at the same versions.

| Project | Author or organization | URL | Version | License | Modifications | Required notices |
|---|---|---|---|---|---|---|
| beautifulsoup4 | Leonard Richardson | https://www.crummy.com/software/BeautifulSoup/ | 4.15.0 | MIT | None | License text shipped with the package |
| charset-normalizer | Ahmed TAHRI | https://github.com/jawah/charset_normalizer | 3.5.2 | MIT | None | License text shipped with the package |
| fastembed | Qdrant | https://github.com/qdrant/fastembed | 0.9.1 | Apache-2.0 | None | License and notices shipped with the package |
| filelock | Bernát Gábor | https://github.com/tox-dev/py-filelock | 4.2.4 | MIT | None | License text shipped with the package |
| flatbuffers | Google LLC | https://github.com/google/flatbuffers | 25.12.19 | Apache-2.0 | None | License and notices shipped with the package |
| fsspec | Martin Durant (Anaconda) | https://github.com/fsspec/filesystem_spec | 2026.9.0 | BSD-3-Clause | None | License text shipped with the package |
| hf-xet | Hugging Face, Inc. | https://github.com/huggingface/xet-core | 1.8.0 | Apache-2.0 | None | License and notices shipped with the package |
| huggingface-hub | Hugging Face, Inc. | https://github.com/huggingface/huggingface_hub | 1.33.0 | Apache-2.0 | None | License and notices shipped with the package |
| loguru | Delgan | https://github.com/Delgan/loguru | 0.7.3 | MIT | None | License text shipped with the package |
| mmh3 | Hajime Senuma | https://github.com/hajimes/mmh3 | 5.3.1 | MIT | None | License text shipped with the package |
| numpy | NumPy Developers | https://github.com/numpy/numpy | 2.5.5 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | None | License text shipped with the package |
| onnxruntime | Microsoft Corporation | https://github.com/microsoft/onnxruntime | 1.32.0 | MIT | None | License text shipped with the package |
| packaging | Donald Stufft and PyPA | https://github.com/pypa/packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | None | License and notices shipped with the package |
| pillow | Jeffrey A. Clark and contributors | https://github.com/python-pillow/Pillow | 13.0.0 | MIT-CMU | None | License text shipped with the package |
| protobuf | Google LLC | https://github.com/protocolbuffers/protobuf | 7.36.2 | BSD-3-Clause | None | License text shipped with the package |
| pypdf | Mathieu Fenniak and contributors | https://github.com/py-pdf/pypdf | 7.1.1 | BSD-3-Clause | None | License text shipped with the package |
| py-rust-stemmers | Qdrant | https://github.com/qdrant/py-rust-stemmers | 0.1.8 | MIT | None | License text shipped with the package |
| pyyaml | Kirill Simonov and contributors | https://github.com/yaml/pyyaml | 6.0.3 | MIT | None | License text shipped with the package |
| requests | Kenneth Reitz and contributors | https://github.com/psf/requests | 2.34.2 | Apache-2.0 | None | License and notices shipped with the package |
| soupsieve | Isaac Muse | https://github.com/facelessuser/soupsieve | 4.1.2 | MIT | None | License text shipped with the package |
| sqlite-vec | Alex Garcia | https://github.com/asg017/sqlite-vec | 0.1.9 | MIT OR Apache-2.0 | None | License and notices shipped with the package |
| tokenizers | Hugging Face, Inc. | https://github.com/huggingface/tokenizers | 0.23.3 | Apache-2.0 | None | License and notices shipped with the package |
| tqdm | tqdm developers | https://github.com/tqdm/tqdm | 4.70.1 | MPL-2.0 AND MIT | None | Source available at the project URL, as MPL-2.0 requires; license texts shipped with the package |
| urllib3 | Andrey Petrov and contributors | https://github.com/urllib3/urllib3 | 2.8.0 | MIT | None | License text shipped with the package |
| win32-setctime (Windows only) | Delgan | https://github.com/Delgan/win32-setctime | 1.2.1 | MIT | None | License text shipped with the package |

The embedding model `intfloat/multilingual-e5-large` (MIT, by Liang Wang et al.), in the
ONNX conversion published by Qdrant on Hugging Face (`Qdrant/multilingual-e5-large-onnx`),
is downloaded only by the explicit command `lastro sources model prepare`, into the private
data root. It is not distributed with Lastro, and sync or queries never download it.

## Optional retrieval evaluation dependencies

Installed only with the `retrieval-eval` extra (`uv sync --extra retrieval-eval` or
`pip install 'lastro[retrieval-eval]'`), which also brings the `sources` and `eval` extras.
Ragas only scores retrieval quality; it never runs during an exploration.

| Project | Author or organization | URL | Version | License | Modifications | Required notices |
|---|---|---|---|---|---|---|
| ragas | Vibrant Labs (Exploding Gradients) and contributors | https://github.com/vibrantlabsai/ragas | 0.4.4 | Apache-2.0 | None | License and notices shipped with the package |
| aiohappyeyeballs | J. Nick Koston | https://github.com/aio-libs/aiohappyeyeballs | 2.7.1 | PSF-2.0 | None | License text shipped with the package |
| aiohttp | aiohttp team | https://github.com/aio-libs/aiohttp | 3.14.5 | Apache-2.0 AND MIT | None | License and notices shipped with the package |
| aiosignal | aiohttp team | https://github.com/aio-libs/aiosignal | 1.4.0 | Apache-2.0 | None | License and notices shipped with the package |
| appdirs | Trent Mick | https://github.com/ActiveState/appdirs | 1.4.4 | MIT | None | License text shipped with the package |
| datasets | Hugging Face, Inc. | https://github.com/huggingface/datasets | 5.2.0 | Apache-2.0 | None | License and notices shipped with the package |
| dill | Mike McKerns | https://github.com/uqfoundation/dill | 0.4.1 | BSD-3-Clause | None | License text shipped with the package |
| diskcache | Grant Jenks | https://github.com/grantjenks/python-diskcache | 5.6.3 | Apache-2.0 | None | License and notices shipped with the package |
| distro | Nir Cohen | https://github.com/python-distro/distro | 1.9.0 | Apache-2.0 | None | License and notices shipped with the package |
| docstring-parser | Marcin Kurczewski | https://github.com/rr-/docstring_parser | 0.18.0 | MIT | None | License text shipped with the package |
| frozenlist | aiohttp team | https://github.com/aio-libs/frozenlist | 1.8.0 | Apache-2.0 | None | License and notices shipped with the package |
| httpcore2 | Tom Christie and Pydantic | https://github.com/pydantic/httpx2 | 2.16.0 | BSD-3-Clause | None | License text shipped with the package |
| httpx2 | Tom Christie and Pydantic | https://github.com/pydantic/httpx2 | 2.16.0 | BSD-3-Clause | None | License text shipped with the package |
| instructor | Jason Liu, Ivan Leo | https://github.com/567-labs/instructor | 1.17.0 | MIT | None | License text shipped with the package |
| jiter | Samuel Colvin (Pydantic) | https://github.com/pydantic/jiter | 0.14.0 | MIT | None | License text shipped with the package |
| jsonpatch | Stefan Kögl | https://github.com/stefankoegl/python-json-patch | 1.41 | BSD-3-Clause | None | License text shipped with the package |
| jsonpointer | Stefan Kögl | https://github.com/stefankoegl/python-json-pointer | 3.8 | BSD-3-Clause | None | License text shipped with the package |
| langchain, langchain-classic, langchain-core, langchain-openai, langchain-text-splitters | LangChain, Inc. | https://github.com/langchain-ai/langchain | 1.5.2, 1.0.8, 1.8.3, 1.8.3, 1.1.3 | MIT | None | License text shipped with the package |
| langchain-community | LangChain, Inc. | https://github.com/langchain-ai/langchain-community | 0.4.2 | MIT | None | License text shipped with the package |
| langchain-protocol | LangChain, Inc. | https://github.com/langchain-ai/agent-protocol | 0.0.20 | MIT | None | License text shipped with the package |
| langgraph, langgraph-checkpoint, langgraph-prebuilt, langgraph-sdk | LangChain, Inc. | https://github.com/langchain-ai/langgraph | 1.2.19, 4.3.1, 1.1.1, 0.4.12 | MIT | None | License text shipped with the package |
| langsmith | LangChain, Inc. | https://github.com/langchain-ai/langsmith-sdk | 0.14.17 | MIT | None | License text shipped with the package |
| multidict | Andrew Svetlov | https://github.com/aio-libs/multidict | 7.2.1 | Apache-2.0 | None | License and notices shipped with the package |
| multiprocess | Mike McKerns | https://github.com/uqfoundation/multiprocess | 0.70.19 | BSD-3-Clause | None | License text shipped with the package |
| nest-asyncio | Ewald R. de Wit | https://github.com/erdewit/nest_asyncio | 1.6.0 | BSD-2-Clause | None | License text shipped with the package |
| networkx | NetworkX Developers | https://github.com/networkx/networkx | 3.7 | BSD-3-Clause | None | License text shipped with the package |
| openai | OpenAI | https://github.com/openai/openai-python | 3.3.0 | Apache-2.0 | None | License and notices shipped with the package |
| orjson | ijl | https://github.com/ijl/orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | None | Source available at the project URL, as MPL-2.0 requires; license texts shipped with the package |
| ormsgpack | Aviram Hassan, Emanuele Giaquinta | https://github.com/ormsgpack/ormsgpack | 1.13.0 | Apache-2.0 OR MIT | None | License and notices shipped with the package |
| pandas | The Pandas Development Team | https://github.com/pandas-dev/pandas | 3.1.1 | BSD-3-Clause | None | License text shipped with the package |
| propcache | Andrew Svetlov | https://github.com/aio-libs/propcache | 0.5.4 | Apache-2.0 | None | License and notices shipped with the package |
| pyarrow | Apache Arrow Developers | https://github.com/apache/arrow | 26.0.1 | Apache-2.0 | None | License and notices shipped with the package |
| python-dateutil | Gustavo Niemeyer and contributors | https://github.com/dateutil/dateutil | 4.0.0 | BSD-3-Clause | None | License text shipped with the package |
| regex | Matthew Barnett | https://github.com/mrabarnett/mrab-regex | 2026.11.19 | Apache-2.0 AND CNRI-Python | None | License and notices shipped with the package |
| requests-toolbelt | Ian Cordasco, Cory Benfield | https://github.com/requests/toolbelt | 1.0.0 | Apache-2.0 | None | License and notices shipped with the package |
| scikit-network | Scikit-network team | https://github.com/sknetwork-team/scikit-network | 0.33.5 | BSD-3-Clause | None | License text shipped with the package |
| scipy | SciPy Developers | https://github.com/scipy/scipy | 1.18.1 | BSD-3-Clause | None | License text shipped with the package |
| sniffio | Nathaniel J. Smith | https://github.com/python-trio/sniffio | 1.3.1 | MIT OR Apache-2.0 | None | License and notices shipped with the package |
| sqlalchemy | Mike Bayer and contributors | https://github.com/sqlalchemy/sqlalchemy | 2.1.9 | MIT | None | License text shipped with the package |
| tenacity | Julien Danjou | https://github.com/jd/tenacity | 9.2.1 | Apache-2.0 | None | License and notices shipped with the package |
| tiktoken | OpenAI, Shantanu Jain | https://github.com/openai/tiktoken | 0.14.0 | MIT | None | License text shipped with the package |
| truststore | Seth Michael Larson, David Glick | https://github.com/sethmlarson/truststore | 0.10.5 | MIT | None | License text shipped with the package |
| uuid-utils | Amin Alaee | https://github.com/aminalaee/uuid-utils | 0.19.0 | BSD-3-Clause | None | License text shipped with the package |
| websockets | Aymeric Augustin and contributors | https://github.com/python-websockets/websockets | 17.4 | BSD-3-Clause | None | License text shipped with the package |
| xxhash | Yue Du | https://github.com/ifduyue/python-xxhash | 4.0.2 | BSD-2-Clause | None | License text shipped with the package |
| yarl | Andrew Svetlov | https://github.com/aio-libs/yarl | 1.25.1 | Apache-2.0 | None | License and notices shipped with the package |
| zstandard | Gregory Szorc | https://github.com/indygreg/python-zstandard | 0.25.0 | BSD-3-Clause | None | License text shipped with the package |

In the repository lockfile these packages share one resolution with the rest of the
project, so the locked development environment uses rich 14.3.4, as Ragas requires. A plain
`pip install lastro` resolves its own versions and is not affected.

## Browser protocol dependencies

| Project | Author or organization | URL | Version | License | Modifications | Required notices |
|---|---|---|---|---|---|---|
| mcp | Anthropic, PBC. | https://github.com/modelcontextprotocol/python-sdk | 1.30.0 | MIT | None | License text shipped with the package |
| attrs | Hynek Schlawack <hs@ox.cx> | https://github.com/python-attrs/attrs | 26.1.0 | MIT | None | License text shipped with the package |
| httpx-sse | Florimond Manca <florimond.manca@protonmail.com> | https://github.com/florimondmanca/httpx-sse | 0.4.3 | MIT | None | License text shipped with the package |
| jsonschema | Julian Berman <Julian+jsonschema@GrayVines.com> | https://github.com/python-jsonschema/jsonschema | 4.26.0 | MIT | None | License text shipped with the package |
| jsonschema-specifications | Julian Berman <Julian+jsonschema-specifications@GrayVines.com> | https://github.com/python-jsonschema/jsonschema-specifications | 2025.9.1 | MIT | None | License text shipped with the package |
| pydantic-settings | Samuel Colvin <s@muelcolvin.com>, Eric Jolibois <em.jolibois@gmail.com>, Hasan Ramezani <hasan.r67@gmail.com> | https://github.com/pydantic/pydantic-settings | 2.16.2 | MIT | None | License text shipped with the package |
| pywin32 | Mark Hammond (et al) | https://github.com/mhammond/pywin32 | 312 | PSF | None | License text shipped with the package |
| referencing | Julian Berman <Julian+referencing@GrayVines.com> | https://github.com/python-jsonschema/referencing | 0.37.0 | MIT | None | License text shipped with the package |
| rpds-py | Julian Berman <Julian+rpds@GrayVines.com> | https://github.com/sponsors/Julian | 2026.9.1 | MIT | None | License text shipped with the package |
| sse-starlette | sysid <sysid@gmx.de> | https://github.com/sysid/sse-starlette | 3.6.0 | BSD-3-Clause | None | License text shipped with the package |
