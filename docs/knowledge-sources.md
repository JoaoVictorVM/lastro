# Knowledge sources: documentation retrieval

Lastro can index a local folder of system documentation and give the agent short, cited passages from it during a run. Documentation is a declared source: it says what the system should do, never what it does, and it never changes a policy, an approval or the plan. When documentation disagrees with what the agent observes, with the issue, with remembered facts or with a human answer, the run and the report show both versions without choosing one.

This release covers documentation ingestion, hybrid retrieval with citations, conflict reporting and retrieval evaluation. Knowledge cards inferred from C#/.NET and Vue code are not available yet; see the [roadmap](../README.md#roadmap).

## Install and prepare

The feature lives in the optional `sources` extra, so the base installation stays small:

```sh
uv sync --extra sources            # or: pip install 'lastro[sources]'
lastro sources model prepare       # explicit download of the local embedding model
lastro doctor                      # "Fontes documentais" reports the extra and the model
```

`model prepare` downloads `intfloat/multilingual-e5-large` (about 2.2 GB, MIT license) into the private data root, outside the history backups, and records the hash of every file. Sync and queries load the model only from that cache with `local_files_only`; a missing or altered model is an actionable error (`model_not_ready`), never an implicit download and never a silent fallback to keyword search. Sync also requires `gitleaks`, the same scanner used for change context.

## Commands

```sh
lastro sources add docs/ --project demo   # register a folder and index it
lastro sources sync                       # re-index changed files, drop deleted ones
lastro sources sync --source-id <id>      # one folder only
lastro sources sync --rebuild             # redo extraction, chunks and vectors
lastro sources list --json                # ids, labels and counts, never absolute paths
```

`--project` accepts the folder name of `lastro.toml` or the configured `project_id`; any other value fails before reading. Repeating `add` for the same folder runs an idempotent sync. Overlapping folders are refused. Exit codes: `0` success, `1` operational failure or partial sync, `2` invalid input, `130` interrupted (the previous index stays active).

Example:

```text
✓ Fonte 'docs' (src_...) sincronizada: geração 2
  Arquivos: 1 adicionado, 1 atualizado, 1 removido, 8 inalterados, 0 excluídos, 0 com falha
  Chunks ativos: 34. Tempo: 12.4s
```

## What is indexed

- Formats: Markdown (`.md`, `.markdown`), plain text (`.txt`), HTML (`.html`, `.htm`) and PDF with a text layer, case-insensitive. Text must be UTF-8.
- Limits per project: 2,000 eligible files and 200,000,000 original bytes, checked from metadata before any extraction. Exceeding either aborts the whole sync (`corpus_limit_exceeded`) and keeps the active index.
- Reading follows the change-context guarantees: no symlinks, junctions or special files, no escape from the folder, credential-like names denied, files that change during reading excluded. A possible secret found by `gitleaks` in the extracted text excludes the whole document, including its previous indexed version; its text and raw hash are never stored.
- HTML is static text: scripts, styles, templates, comments and hidden content are dropped, and no link, stylesheet, image or font is fetched. PDF is extracted in a separate process limited to 60 seconds and 512 MiB, without JavaScript, attachments or passwords; encrypted, corrupted or image-only PDFs are reported (`encrypted_document`, `extraction_failed`, `no_text_layer`). Up to 10,000,000 characters are extracted per file.
- Chunks have up to 800 tokens with 100 tokens of overlap and never cross a document, a heading or a PDF page. Each chunk stores the path, heading, line or page range, offsets and content hash. Each chunk is embedded in windows of at most 480 tokens, so the end of a chunk is never truncated.
- Sync always hashes every file: a changed file with the same modification time is still re-indexed, and an untouched file with a new modification time only updates metadata. A new generation is prepared outside the publication and becomes active in one short transaction guarded by a lease, so queries never see a mix of old and new chunks. An unreachable folder aborts the sync without deleting anything; a file that now fails extraction leaves the search with a warning.

## Retrieval in a run

When a project has indexed sources, `mission_context` shows `sources.available`. After the plan is accepted, the agent calls `sources_query` with a flow key and a question (up to 1,000 characters and 200 tokens). Lastro combines SQLite FTS5 (BM25) and the local vectors with Reciprocal Rank Fusion (k = 60, 20 candidates from each side, equal weights), filters by the active generation before limiting candidates, and returns at most 5 whole passages within 4,000 tokens and 48,000 characters. Every passage carries a citation such as `[docs/cadastro.md § Campos obrigatórios]`, the file hash and modification date, `source_type: documentation`, `authority: declared` and `deployment_match: unknown`. The passages are recorded before they are returned; if recording fails, nothing is returned (`sources_unavailable`). No score is presented as confidence.

Findings whose expected result comes from documentation must cite a passage delivered in the same run (`source: documentation`, `source_ref` with the passage `reference_id`). Reproduction attempts receive a copy of that passage in their brief, marked as a declared expectation; they have no documentation search of their own.

## Conflicts

- An author can declare typed facts in a Markdown block marked `lastro-fact` (flow, category and a memory fact, nothing else). They enter the memory as `documentation`, never as observed or user-confirmed, with the document as provenance. Project-wide documentation is compared with more specific remembered values; a divergent value marks both entries `contested` and records a `structured` conflict. Invalid blocks are ignored with `fact_metadata_invalid`; the text stays searchable.
- For prose, the agent reports a divergence with `sources_report_conflict`: two references from the authorized context of the run (a delivered passage, a consulted memory entry, a mission criterion, a human answer, an observation of the run or a controlled source file), a literal quote of each and a short explanation. The quotes prove where the text came from, not that the interpretation is right: the conflict stays `reported`.
- Conflicts never pick a winner. Removing a document with `sources sync` takes it out of automatic use but resolves nothing; only a human decision in `lastro knowledge` closes them.

## Reports

The private report lists the sources used by each finding (citation, file hash, date and the passage as delivered), separates declared expectations from observed behavior, and has a `Source conflicts` section with both versions. Reports stay readable after the documents change or disappear, because they use the snapshots recorded in the run. Older documentation references that cannot be verified are flagged. `lastro report export --public` keeps only the source type and file hash: no passage text, file names, headings or paths.

## Evaluating retrieval

`evals/suites/retrieval-demo.yaml` measures retrieval over the synthetic demo documentation in `evals/retrieval/demo/` (34 reviewed questions with reference answers and anchors; the documentation intentionally states that the email is optional while the demo requires it). Install the `retrieval-eval` extra, which adds Ragas, and run it like any other suite:

```sh
uv sync --extra retrieval-eval
lastro eval run evals/suites/retrieval-demo.yaml
lastro eval resume <suite run id>
```

The corpus is indexed in an index owned by the suite, never in the project sources. Each question and repetition is a trial: Lastro retrieves the top 5 with the production service, generates the answer in a new headless session without tools that sees only the question and the passages, and scores it with Ragas (`retrieval.context_precision`, `retrieval.context_recall`, `retrieval.faithfulness`) through the configured judge. Every model call, including Ragas retries, is reserved and settled in the suite budget; a provider limit or the budget cap pauses the suite (exit 4) and `resume` continues without redoing finished questions. Resume refuses a changed question set, corpus, prompt, executor, judge or Ragas version.

Results go to the same evaluation store as the exploration suites, with mean, sample standard deviation, valid N, planned N and the reasons for any excluded score. A score Ragas could not produce is excluded with its reason and never counted as zero. Per-question means are exported too. There is no quality threshold: the suite reports what it measured. It sends only the synthetic demo documentation to the judge provider.
