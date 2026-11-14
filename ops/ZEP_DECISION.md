# Memory layer (Zep) decision

**Status: DECIDED 2026-11-14: option (a), local drop-in adapter. Implemented in `ops/patches/0001-local-memory-backend.patch`.**

**Requirement:** seed content must not leave this machine.

**Recommendation (accepted): option (a), a local drop-in adapter backed by an embedded SQLite + numpy store, with embeddings from Ollama.** Fall back to option (c) only if the pilot shows local extraction quality is unusable. Do not adopt option (b).

## 0. What was built

Enabled with `MIROFISH_MEMORY_BACKEND=local` in `mirofish/.env` (see `ops/config/mirofish.env.example`). Unset, upstream uses Zep Cloud exactly as before.

| Module (`backend/app/local_graph/`) | Role |
|---|---|
| `client.py` | `LocalZep`: the 17 zep-cloud methods MiroFish calls, same names, arguments and return attributes; missing objects raise `zep_cloud.NotFoundError` |
| `store.py` | SQLite file `backend/uploads/local_graph/graph.sqlite3` (directory 0700 when created by us, file and `.lock` file 0600, rollback journal so no `-wal` side file). `secure_delete` is on, so `graph.delete` overwrites the deleted rows with zeros. An exclusive lock file limits the database to one backend process |
| `extractor.py` | Ontology-constrained extraction with MiroFish's own `LLMClient` (i.e. local Ollama). Source text is delimited and declared to be data. Types outside the ontology become `Entity` / `RELATED_TO` (never free-form labels), as does a relation whose endpoint types its `source_targets` do not allow. An entity is kept only if its name occurs in the source text, which blocks hallucinated and injected entities |
| `ingest.py` | Groups chunks into windows (≤ 4,000 chars, ≤ 8 chunks; one LLM call each), resolves entities (exact normalised name, else same type + shared word + embedding cosine ≥ 0.90), merges facts, builds summaries from facts. Model calls run outside the database lock; each window's writes commit in one transaction that first checks the graph still exists, so deleting a graph mid-build leaves nothing behind |
| `embedder.py` | Ollama `/api/embed`; refuses non-loopback URLs and ignores proxy variables |
| `search.py` | BM25 + cosine, fused by reciprocal-rank fusion (k = 60) |
| `worker.py` | Background thread emulating Zep's async Batch API and `graph.add`; resumes interrupted batches on restart |

Failure policy:
- A chunk whose extraction returns unusable JSON is recorded on the episode (`error = "extraction_failed: …"`) and the build continues, unless more than half the chunks failed (`LOCAL_GRAPH_MAX_EXTRACTION_FAILURE_RATIO`, default 0.5), which fails the build. `LOCAL_GRAPH_STRICT_EXTRACTION=1` fails it on the first bad chunk.
- An Ollama or embedding outage always fails the build, so a build can never "succeed" with an empty graph.
- MiroFish waits up to `LOCAL_GRAPH_INGESTION_TIMEOUT_SECONDS` (default 7,200; upstream's Cloud value is 600) for ingestion.

Privacy guard: in local mode the backend refuses to start (`Config.validate()`) unless `LLM_BASE_URL`, and `LLM_BOOST_BASE_URL` if set, point at a loopback host. Every prompt (extraction, personas, simulation, report) carries seed-derived text, so this is what actually enforces "seed content does not leave the machine" on the LLM side.

Smoke test with the real model (2026-11-14, synthetic 5-sentence document, 2 chunks, 9 s): 7 of 7 named actors extracted with correct types and attributes; 4 of the 5 stated relationships (one "defended" relation was missed); fact search ranked the relevant fact first; deleting the graph left zero rows. One fact was mislabelled with an out-of-constraint type, which led to the `source_targets` check above.

Not reproduced from Zep Cloud: cross-encoder reranking (both rerankers use the fusion above), LLM-written node summaries, temporal invalidation of superseded facts (`invalid_at`/`expired_at` stay null), episode-scope search (rejected with a 400 error; MiroFish does not use it).

## 1. What MiroFish actually needs from Zep

Full detail is in `ops/NOTES.md` section 6. In summary:

- **Call surface:** 17 SDK methods across 6 modules, all obtained through one factory, `get_zep_client()` in `backend/app/utils/zep.py`:
  - graph lifecycle: `graph.create`, `graph.get`, `graph.delete`, `graph.set_ontology`;
  - ingestion: `batch.create`, `batch.add`, `batch.process`, `batch.get`, `batch.list`, `batch.list_items`, `graph.add`, `graph.episode.get`;
  - reads: `graph.node.get`, `graph.node.get_edges`, paged `graph.node/edge.with_raw_response.get_by_graph_id`;
  - search: `graph.search` with scope `edges` or `nodes`, rerankers `rrf` and `cross_encoder`.
- **Small, single entry point.** Because every call site goes through `get_zep_client()`, one factory switch can substitute a local client without touching the six calling modules.
- **But Zep does work, not just storage.** MiroFish uploads raw text and relies on Zep Cloud to:
  1. extract typed entities and relationships with an LLM, constrained by the ontology;
  2. resolve duplicate entities across chunks;
  3. write node summaries;
  4. timestamp facts;
  5. run hybrid search with reranking.

  Any local replacement has to reimplement 1, 2, 3 and 5 with the local LLM. **This is the real cost of going local.** Agent count and persona quality depend directly on step 1, because one agent is created per typed entity.
- **Data that leaves the machine under upstream Zep Cloud:**
  - every 500-character seed chunk;
  - the ontology;
  - the project name;
  - search queries (they contain entity names and the report's questions);
  - if graph-memory update is on (the UI always enables it), every simulated post, comment and follow, with agent names.

## 2. Options evaluated (in the order you asked)

### (a) Local drop-in adapter (recommended)

**Design: a patch to upstream, selected by `MIROFISH_MEMORY_BACKEND=local`.**

- `utils/zep.py`, about 10 changed lines: `get_zep_client()` returns a `LocalZep` object instead of the Cloud client.
- `config.py`: skip the `ZEP_API_KEY` requirement in local mode.
- New package `backend/app/local_graph/`, implementing the same 17 methods and returning objects with the same attribute names MiroFish reads (`uuid_`, `name`, `labels`, `summary`, `fact`, `source_node_uuid`, the `zep-next-cursor` paging header, batch `status`/`progress`, and so on):

| Part | What it does |
|---|---|
| Store | Embedded SQLite file under `backend/uploads/local_graph/` (mode 0600), with numpy for vector maths. Tables: graphs, ontology, episodes, nodes, edges, batches. No server and no network port. |
| Extraction | Uses MiroFish's own `LLMClient.chat_json` against Ollama, prompted with the registered ontology. Several chunks per call to limit cost. Entity and edge types not in the ontology are **rejected** (the fork accepts them; see (b)). |
| Entity resolution | Normalised-name match plus an embedding-similarity threshold. |
| Summaries | Built from each entity's facts by default (no extra LLM calls). An optional LLM summary pass costs one call per entity. |
| Search | Brute-force cosine similarity over Ollama embeddings plus keyword (BM25-style) scoring, fused with reciprocal-rank fusion. `cross_encoder` maps to the same fusion; there is no local cross-encoder. |
| Batch/episode API | Emulated with a background worker, so upstream's polling, reconciliation and lifecycle locks run unchanged. |
| Temporal fields | `created_at` and `valid_at` come from the episode time; `invalid_at` and `expired_at` stay null. The fork does the same. |

**Why SQLite rather than Neo4j Community:**

- Neo4j needs Docker or a Java 17/21 install, and neither is present on this machine.
- It adds a second server, a port and a password.
- Its relationship vector index needs Neo4j 5.18 or later.
- MiroFish graphs are small (hundreds to low thousands of nodes), so brute-force search is fast enough.
- `sqlite3` is in the standard library, and `numpy` and `scikit-learn` are already in upstream's `uv.lock`, so **no new dependencies** are needed.
- Purging a run is a row delete or a file delete.

**Effort and verification:**

- Roughly 900 to 1,300 lines plus tests, kept in `ops/patches/`.
- Upstream already ships fake-Zep contract tests (`backend/tests/test_zep_*.py`, 9 files). The adapter must pass them unchanged, plus our own tests.

**Extra LLM load and models:**

- Extraction adds about 1 LLM call per 3 to 5 chunks. A 50,000-character seed is about 110 chunks, so about 25 to 40 calls.
- With graph-memory update on, each activity batch adds one extraction call. I recommend running with it **off**.
- A second, small model is needed for embeddings, pulled through Ollama. Its repo name is a variable you verify, for example a GGUF of `Qwen3-Embedding-0.6B` or `nomic-embed-text`.

**Risks:**

1. Extraction and deduplication will be weaker than Zep's cloud pipeline. Expect more duplicate or missed entities, which feeds straight into agent count and persona quality. The pilot will measure this.
2. Upstream changes its Zep code often (four Zep-related merged PRs in 2026: #730, #738, #739, #740). The patch may need rebasing when you move the pin.
3. Reranking quality is lower than Zep's cross-encoder.

### (b) Third-party offline fork: `nikmcfly/MiroFish-Offline`

I cloned it to `_review/MiroFish-Offline` and read it, but never ran or installed it. Reviewed commit: `313fe64` (2026-03-24), with full history (22 commits).

| Aspect | Finding |
|---|---|
| License | AGPL-3.0, same as upstream, with attribution. Compatible. |
| Approach | Replaces every Zep call with a `GraphStorage` abstraction backed by Neo4j CE (`backend/app/storage/`, about 1,560 lines): synchronous LLM entity/relation extraction via Ollama, `nomic-embed-text` embeddings, hybrid search scored 0.7 × vector + 0.3 × BM25, no reranker, dedup by lowercase name only. All prompts and the UI translated and forced to English. |
| Divergence from upstream | Forked from upstream around 2026-03-06 (`985f89f`). **Missing the 103 upstream commits since then**, including:<br>• the fix for **Flask debug defaulting to True while bound to 0.0.0.0** (`e9cac5f`); the fork still has `FLASK_DEBUG` default `'True'` in `backend/app/config.py:25`, which exposes the Werkzeug debugger;<br>• axios/rollup/picomatch vulnerability upgrades (`223b283`);<br>• the NLTK CVE-2025-14009 lockfile fix (`cbfd03d`);<br>• the report-agent fabricated-tool-result sanitiser (`1d96b09`);<br>• ontology and profile normalisation fixes;<br>• the Python `<3.13` constraint (the fork allows 3.13, which upstream says breaks). |
| Security issue found | `neo4j_storage.py:286` interpolates the entity *type* returned by the LLM into Cypher as a label, and `ner_extractor.py` deliberately keeps types that are **not** in the ontology. A seed containing an injection string could therefore steer the model into producing a Cypher-breaking label. Low blast radius (local database), but a real seed-to-database injection path. |
| Docker compose | Publishes Neo4j (7474, 7687) and Ollama (11434) on **all interfaces**, with default password `mirofish`. Uses unpinned `ollama/ollama:latest`. Reserves an NVIDIA GPU, which does not work under Docker on macOS (no Metal passthrough). |
| Tests | None (upstream has 18 backend test files). |
| Maintenance | 22 commits between 2026-03-15 and 2026-03-24, including 4 merge commits. **No activity since, about 8 months.** |
| Hardware (its README) | 16 GB RAM minimum, 32 GB recommended; 10 GB VRAM for a 14B model, 24 GB for 32B; Neo4j heap up to 2 GB; plus the embedding model. This machine (M3 Max, 96 GB unified) exceeds that, but would need Neo4j via Docker or Java, neither of which is installed. |

**Verdict:** useful as a reference design. Its interface and extraction prompt confirm that approach (a) is workable, and reusing ideas or code is licence-compatible because our patches are AGPL anyway. But running it means losing eight months of upstream fixes, inheriting a debug-mode exposure and an injection path, and depending on an unmaintained fork. Not recommended.

### (c) Zep Cloud with a pseudonymised seed (fallback)

- **No code changes; best extraction quality.**
- **What still leaves the machine, even after pseudonymisation:**
  - the structure of the situation (who relates to whom, events, sequence);
  - quotes and paraphrases;
  - the ontology;
  - the report's questions (inside search queries);
  - simulated posts, if graph-memory update is on (must be **off** under this option).
- **Masking figures:** your rule "never raw names, figures or counterparties" means `pseudonymize.py` must also mask numbers and dates, not just names. That degrades simulation fidelity on anything quantitative.
- **Data handling:** the data is processed by Zep, and by whatever LLM providers Zep uses internally, under Zep's terms.
- **Account:** requires a Zep account. The free-tier quota applies.

## 3. Hostnames contacted at runtime under each path

From reading the code. These will be **confirmed empirically by `netcheck.sh` in Phase 3**, and this section will be updated with the measured result.

| Host | (a) local adapter | (b) fork | (c) Zep Cloud | Seed data? |
|---|---|---|---|---|
| `127.0.0.1:11434` (Ollama: chat and embeddings) | yes | yes | yes (chat only) | stays local |
| `127.0.0.1:5001` / `:3000` (backend / frontend) | yes | yes | yes | stays local |
| `localhost:7687` (Neo4j bolt) | no | yes | no | stays local |
| `api.getzep.com` | **no** | no | **yes** | **yes (pseudonymised)** |
| `huggingface.co` + CDN (`Twitter/twhin-bert-base`) | first Twitter run only | same | same | no (model download) |
| `openaipublic.blob.core.windows.net` (tiktoken encodings) | first run only | same | same | no (file download) |
| `fonts.googleapis.com`, `fonts.gstatic.com` (browser) | until patched in Phase 3 | same | same | no (IP and referrer only) |

**Plan for the two one-time downloads under (a):**

1. Fetch them once in a supervised setup step: the Twitter model into a dedicated `HF_HOME`, and the tiktoken files into `TIKTOKEN_CACHE_DIR`.
2. Then set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.
3. Alternatively, run Reddit-only (`platform=reddit`), which loads no Hugging Face model at all.

After that, `ops/config/allowed_hosts.txt` stays **empty** and `netcheck.sh` must see loopback connections only.

### 3.1 Confirmed for the chosen path (a)

Confirmed by reading the patched code. `netcheck.sh` (Phase 3) will add the measured result here.

**MiroFish backend process at runtime:**

| Host | When | Carries seed content? |
|---|---|---|
| `127.0.0.1:11434` | always: chat (`/v1/chat/completions`), embeddings (`/api/embed`) | yes, loopback only |
| `huggingface.co` and its CDN | only if a Twitter simulation runs before `Twitter/twhin-bert-base` is cached; never once `HF_HUB_OFFLINE=1` | no |
| `openaipublic.blob.core.windows.net` | only if tiktoken encodings are not cached in `TIKTOKEN_CACHE_DIR` | no |
| `api.getzep.com` | **never**: `get_zep_client()` returns the local client before the Cloud client is constructed (tested in `test_local_backend_needs_no_zep_key_and_never_builds_cloud_client`) | — |

**Browser (frontend):** `fonts.googleapis.com` and `fonts.gstatic.com` until the Phase 3 font patch. They receive your IP and the page referrer, not seed content.

**Ollama server process** (separate from MiroFish, but on the same machine):

| Host | When | Carries seed content? |
|---|---|---|
| `hf.co` / `huggingface.co` and CDN | only during `ops/scripts/build_model.sh` (model pull) | no |
| `ollama.com` | a "model recommendations" catalog fetch scheduled every few hours (seen in `ops/logs/ollama.log`, `model_recommendations.go`). `OLLAMA_NO_CLOUD=1` disables cloud models but **not** this fetch, and v0.40.16 exposes no setting to turn it off | no; it is a catalog download. It does reveal your IP and Ollama version to ollama.com |

If you want the Ollama fetch blocked as well, it needs an OS-level outbound firewall rule for the Ollama binary. That is a machine policy decision for you, so I have not made it; `netcheck.sh` will report it separately from MiroFish's own traffic.

## 4. Sub-decisions that come with (a) (defaults in brackets)

1. Embedding model to pull through Ollama [`hf.co/Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0`, verified on Hugging Face, alias `mirofish-embed`].
2. Platforms [`parallel`, after pre-caching the Twitter model; `reddit` only if you want zero Hugging Face involvement].
3. Graph-memory update during simulation [off].
4. Node summaries [built from facts, no extra LLM calls].
