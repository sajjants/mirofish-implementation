# MiroFish study notes (upstream commit `7657031`)

Written from reading the source at the pinned commit. Paths are relative to `mirofish/`. Line numbers refer to the pinned commit and will drift if upstream is updated. Nothing from the repo has been executed or installed at the time of writing.

## 0. Public-doc claims checked against the repo

| Claim | Verified? | Evidence |
|---|---|---|
| `README-EN.md` exists | **No.** `README.md` is English, `README-ZH.md` is Chinese. | repo root |
| Node 18+ | Yes | `package.json` `engines.node >=18.0.0` |
| Python 3.11 or 3.12, not 3.13 | Yes | `backend/pyproject.toml` `requires-python = ">=3.11,<3.13"` |
| uv as package manager | Yes | `package.json` `setup:backend` = `cd backend && uv sync`; `backend/uv.lock` exists |
| `.env` vars `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_NAME`, `ZEP_API_KEY` | Yes, plus optional `LLM_BOOST_*` | `.env.example`, `backend/app/config.py` |
| `npm run setup:all` then `npm run dev` | Yes | `package.json` |
| Frontend 3000, backend 5001, `/health` | Yes | `frontend/vite.config.js` (`port: 3000`), `backend/run.py` (`FLASK_PORT` default 5001), `backend/app/__init__.py:72-74` returns `{"status":"ok","service":"MiroFish Backend"}` |
| `docker compose up -d` alternative | Yes, but it pulls a prebuilt image `ghcr.io/666ghj/mirofish:latest` (unpinned tag) rather than building locally | `docker-compose.yml` |
| Upload PDF, Markdown, text up to 50 MB | Yes: `pdf`, `md`, `txt`, `markdown`; 50 MB is the whole request | `backend/app/config.py:36-38` |
| Default max rounds 10 via `OASIS_DEFAULT_MAX_ROUNDS` | **Defined but never used.** Rounds come from the LLM-generated time config, optionally cut by `max_rounds` on `POST /api/simulation/start`. | `config.py:45`; no other reference in `backend/` |
| Stay under 40 rounds until cost is known | Yes (comment in `.env.example`, README). The UI's round slider also defaults to 40. | `.env.example:3`, `frontend/src/components/Step2EnvSetup.vue:676` |
| Requires Zep Cloud; self-hosted Zep not supported | Yes, and it is **actively enforced**: setting `ZEP_API_URL` makes startup fail. | `config.py:71-72`, `backend/app/utils/zep.py:21,68-72` |

## 1. The five-stage flow

All stages are driven by REST calls from the Vue frontend to the Flask backend. Every stage is also callable directly over HTTP, so a wrapper script can run the whole pipeline without the UI. The full route inventory is in section 9.

1. **Graph building**
   - `POST /api/graph/ontology/generate` (multipart: `files`, `simulation_requirement`). The backend extracts text from the uploaded files (PyMuPDF for PDF, charset detection for text) and saves it as `uploads/projects/<project_id>/extracted_text.txt`. One LLM call (JSON mode) designs an *ontology*: up to 10 entity types and 10 edge types, each with up to 10 attributes (`backend/app/utils/ontology.py:6-8`). Seeds longer than 50,000 characters are sampled down to at most 60 excerpts for this call (`ontology_generator.py:251-377`).
   - `POST /api/graph/build` (async, poll `GET /api/graph/task/<task_id>`). The text is split into 500-character chunks with 50-character overlap. MiroFish creates a Zep graph, registers the ontology with Zep, and uploads every chunk to Zep as a "graph episode" through Zep's Batch API. **Zep Cloud, not MiroFish, then runs the entity and relationship extraction** on its own infrastructure. MiroFish polls until Zep finishes, then reads back node and edge counts.
2. **Environment setup**
   - `POST /api/simulation/create` registers a simulation for the project (Twitter and Reddit both enabled by default).
   - `POST /api/simulation/prepare` (async). Reads every node and edge from Zep, keeps nodes that carry an ontology type label, and generates one persona per kept node (section 3). Then an LLM designs the simulation config: total simulated hours, minutes per round, activity schedule, hot topics, seed posts, and per-agent activity, stance and influence (section 4).
3. **Simulation**
   - `POST /api/simulation/start` launches a subprocess (`backend/scripts/run_parallel_simulation.py` by default) that runs CAMEL-AI **OASIS** social-media simulations for Twitter and Reddit concurrently. Each round, a random subset of "active" agents takes one LLM tool-calling action (post, like, repost, comment, follow, and so on).
   - If `enable_graph_memory_update` is true (the UI always sends true; the API default is false), agent actions are converted to natural-language text and pushed back into the Zep graph as new episodes, so Zep extracts new facts from the simulated posts.
   - After the last round the process stays alive waiting for interview commands. It only reports `completed` after `POST /api/simulation/close-env` or `stopped` after `/stop`.
4. **Report generation**
   - `POST /api/report/generate` (async). A ReAct-style ReportAgent plans 2 to 5 sections, then writes each section using tools: `insight_forge` (sub-query decomposition plus Zep searches), `panorama_search` (whole graph), `quick_search` (one Zep search), and `interview_agents` (asks live simulated agents questions over file-based IPC). This needs the simulation `completed` or `stopped`.
5. **Deep interaction**
   - `POST /api/report/chat` chats with the ReportAgent (works after the simulation process has exited).
   - `POST /api/simulation/interview`, `/interview/batch`, `/interview/all` question individual agents. These need the simulation process still alive in wait mode.

## 2. Configuration variables

### Read by MiroFish code

| Variable | Default | Used? | Notes |
|---|---|---|---|
| `LLM_API_KEY` | none (required) | yes | Startup fails if empty. Any non-empty string works for Ollama. Also exported by the simulation scripts as `OPENAI_API_KEY` for CAMEL. |
| `LLM_BASE_URL` | code: `https://api.openai.com/v1`; `.env.example`: `https://dashscope.aliyuncs.com/compatible-mode/v1` | yes | Exported as `OPENAI_API_BASE_URL` for CAMEL in the simulation subprocess. |
| `LLM_MODEL_NAME` | code: `gpt-4o-mini`; `.env.example`: `qwen-plus` | yes | Same model for ontology, personas, config, simulation agents, report and chat. |
| `ZEP_API_KEY` | none (required) | yes | Startup fails if empty. |
| `ZEP_API_URL` | must be unset | yes (as a guard) | Startup fails if set. Prevents pointing the SDK at self-hosted Zep. |
| `LLM_BOOST_API_KEY`, `LLM_BOOST_BASE_URL`, `LLM_BOOST_MODEL_NAME` | unset | yes, Reddit simulation only | `run_parallel_simulation.py:984-1009,1322`. **Pitfall:** `.env.example` ships placeholder values for these. Copying it unchanged makes the Reddit simulation call a bogus endpoint. The `.env.example` comment says to delete these lines if unused. |
| `FLASK_HOST` | `0.0.0.0` | yes | `backend/run.py:40`. Binds on all interfaces by default. |
| `FLASK_PORT` | `5001` | yes | `backend/run.py:41` |
| `FLASK_DEBUG` | `False` | yes | `config.py` compares to `'true'`; `simulation_runner.py:1569` compares to `'1'` (inconsistent, harmless when unset). |
| `SECRET_KEY` | `mirofish-secret-key` | loaded into Flask config | No sessions are used, but set a random value anyway. |
| `OASIS_DEFAULT_MAX_ROUNDS` | `10` | **no** | Dead config. |
| `REPORT_AGENT_MAX_TOOL_CALLS` | `5` | **no** | ReportAgent hard-codes its own limits. |
| `REPORT_AGENT_MAX_REFLECTION_ROUNDS` | `2` | **no** | |
| `REPORT_AGENT_TEMPERATURE` | `0.5` | **no** | ReportAgent hard-codes 0.5. |
| `VITE_API_BASE_URL` (frontend) | `http://localhost:5001` | yes | `frontend/src/api/index.js:6` |

### Hard-coded (not configurable without a patch)

`MAX_CONTENT_LENGTH` 50 MB; `ALLOWED_EXTENSIONS` {pdf, md, txt, markdown}; chunk size 500 and overlap 50 (overridable per request on `/api/graph/build` via `chunk_size`, `chunk_overlap`); upload root `backend/uploads`; OASIS action lists; OASIS LLM concurrency semaphore 30; profile-generation parallelism 5 from the UI (request field `parallel_profile_count`); ReportAgent `max_iterations=5`, `MAX_TOOL_CALLS_PER_SECTION=5`, temperature 0.5, `max_tokens=4096`; agent-config batch size 15; ontology limits 10/10/10.

### Read by third-party libraries in the backend process

- `OPENAI_API_KEY`, `OPENAI_API_BASE_URL`: set by the simulation scripts from the `LLM_*` values; read by CAMEL's OpenAI model.
- `AGENTOPS_API_KEY`, `LANGFUSE_ENABLED`, `TRACEROOT_ENABLED`: CAMEL tracing integrations; off unless set.
- `HF_HOME`, `HF_HUB_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY`, `TRANSFORMERS_OFFLINE`: Hugging Face (Twitter recommender model).
- `TIKTOKEN_CACHE_DIR`: where tiktoken caches its encoding files.

The simulation subprocess inherits the **entire** backend environment (`simulation_runner.py:533-535`), including both API keys.

## 3. Personas and agent count

- **Where:** `POST /api/simulation/prepare` → `SimulationManager.prepare_simulation` (`backend/app/services/simulation_manager.py:244`) → `ZepEntityReader.filter_defined_entities` (`backend/app/services/zep_entity_reader.py:221-337`) → `OasisProfileGenerator.generate_profiles_from_entities` (`backend/app/services/oasis_profile_generator.py`).
- **How many agents:** exactly one agent per Zep graph node that carries at least one label other than `Entity`/`Node`, i.e. every node Zep classified into one of the ontology's entity types. **There is no cap**: no env var, config field or request parameter limits the total.
- **The only control** is the optional request field `entity_types` (list of ontology type names) on `/prepare`, which drops nodes of other types. The UI never sends it, so all typed entities become agents.
- **Practical consequence:** agent count is driven by how many distinct named entities the extractor finds in the seed. Trimming or consolidating the seed (fewer named people and organisations) is the main lever; filtering by `entity_types` is the secondary one.
- **Per-agent cost:** 2 Zep searches (edges limit 30, nodes limit 20, `reranker="rrf"`) plus 1 JSON-mode LLM call, retried up to 3 times, then a rule-based fallback profile. Five agents are generated in parallel (UI default).
- **Output:** `twitter_profiles.csv` and `reddit_profiles.json` in the simulation directory. Agent `user_id` is the entity's index.

## 4. Rounds and the LLM-call cost model

- `SimulationConfigGenerator` (`backend/app/services/simulation_config_generator.py`) makes `2 + ceil(N_agents / 15)` JSON-mode LLM calls (each up to 3 attempts):
  1. time config: `total_simulation_hours` (prompt asks 24-168), `minutes_per_round` (30-120, suggests 60), agents active per hour (min, max), peak and off-peak hours. Fallback: 72 h and 60 min;
  2. event config: hot topics, narrative direction, initial posts;
  3. agent configs in batches of 15: activity level, posts and comments per hour, active hours, sentiment bias, stance, influence weight.
- **Total rounds** = `int(total_simulation_hours * 60 / minutes_per_round)`, computed in `SimulationRunner.start_simulation` (`backend/app/services/simulation_runner.py:403-413`). With the LLM's typical choices this is 24 to 336 rounds.
- `max_rounds` on `POST /api/simulation/start` is applied as `min(total_rounds, max_rounds)` in the runner and again in the script (`run_parallel_simulation.py:1220-1224,1419-1423`). **It can only shorten a run, never lengthen it.** This is the knob our planner must set.
- **Per round:** round 0 posts the initial posts without the LLM. Each later round selects active agents (target drawn from the agents-per-hour range, times 1.5 at peak hours or the off-peak multiplier, filtered by each agent's active hours and activity level). Each active agent makes **exactly one** LLM call with tool calling (OASIS `SocialAgent.max_iteration = 1`). Rounds with no active agents are skipped.
- **Platforms:** with `platform=parallel` the Twitter and Reddit simulations run concurrently, so per-round agent calls roughly double.
- **Rough estimate for a run:**
  `calls ≈ 1 (ontology) + N (personas, up to 3N with retries) + (2 + ceil(N/15)) (config) + P × Σ_rounds active_agents(r) + report (≈ 10 to 45) + interviews (2 per agent interviewed when both platforms are live)`
  where P is the number of platforms (1 or 2). Active agents per round are bounded above by `min(N, agents_per_hour_max × 1.5)`.

## 5. Where output is written

All under `backend/uploads/` (git-ignored upstream):

| What | Path |
|---|---|
| Project metadata, ontology, graph id | `uploads/projects/<project_id>/project.json` |
| Uploaded seed files | `uploads/projects/<project_id>/files/` |
| Extracted seed text | `uploads/projects/<project_id>/extracted_text.txt` |
| Graph (nodes, edges, facts, summaries) | **Zep Cloud only.** Nothing is cached locally except `graph_id` and the ontology. |
| Simulation state and config | `uploads/simulations/<sim_id>/state.json`, `simulation_config.json`, `run_state.json` |
| Personas | `uploads/simulations/<sim_id>/twitter_profiles.csv`, `reddit_profiles.json` |
| Simulation transcript | `uploads/simulations/<sim_id>/twitter/actions.jsonl`, `reddit/actions.jsonl`; SQLite `twitter_simulation.db`, `reddit_simulation.db` (tables `post`, `comment`, `user`, `follow`, `trace`) |
| Simulation logs | `uploads/simulations/<sim_id>/simulation.log`, `log/` (OASIS logs) |
| Interview IPC | `uploads/simulations/<sim_id>/ipc_commands/`, `ipc_responses/`, `env_status.json` |
| Reports | `uploads/reports/<report_id>/full_report.md`, `section_NN.md`, `outline.json`, `meta.json`, `progress.json`, `agent_log.jsonl`, `console_log.txt` |
| Backend log | `backend/logs/YYYY-MM-DD.log` (rotating, 10 MB × 5) |
| Async task state | in memory only (lost on restart) |

Implication for purging: a run leaves seed-derived text in at least `projects/`, `simulations/`, `reports/`, `backend/logs/`, and (under upstream) in Zep Cloud. There is no API route to delete a simulation; it must be removed from disk.

## 6. How the memory layer calls Zep

- **SDK:** `zep-cloud==3.25.0`. A single cached client is built in `backend/app/utils/zep.py` (`get_zep_client`), hard-wired to `https://api.getzep.com/api/v2`. `ZEP_API_URL` is rejected.
- **Call sites and methods** (application code, excluding tests and the standalone `scripts/validate_zep_cloud_integration.py`):

| Module | Zep methods |
|---|---|
| `services/graph_builder.py` | `graph.create`, `graph.get`, `graph.set_ontology`, `graph.delete`, `batch.create`, `batch.add`, `batch.process`, `batch.get`, `batch.list`, `batch.list_items`, `graph.episode.get` |
| `utils/zep_paging.py` | `graph.node.with_raw_response.get_by_graph_id`, `graph.edge.with_raw_response.get_by_graph_id` (cursor in `zep-next-cursor` header) |
| `services/zep_entity_reader.py` | `graph.node.get`, `graph.node.get_edges` (+ paging helpers) |
| `services/oasis_profile_generator.py` | `graph.search` (scope `edges` and `nodes`, `reranker="rrf"`) |
| `services/zep_tools.py` (ReportAgent tools) | `graph.search` (`reranker="cross_encoder"`), `graph.node.get` (+ paging helpers) |
| `services/zep_graph_memory_updater.py` | `graph.add` (text episodes), `graph.episode.get` (poll `processed`) |

  That is **17 distinct SDK methods**, plus SDK types used to build requests: `BatchAddItem`, `EntityEdgeSourceTarget`, `EntityModel`, `EntityText`, `EdgeModel` (ontology classes are built dynamically as Pydantic models), and exceptions `NotFoundError`, `ApiError`.
- **Object fields MiroFish reads back:** nodes `uuid_`, `name`, `labels`, `summary`, `attributes`, `created_at`; edges `uuid_`, `name`, `fact`, `fact_type`, `source_node_uuid`, `target_node_uuid`, `attributes`, `created_at`, `valid_at`, `invalid_at`, `expired_at`, `episodes`; episodes `uuid_`, `processed`; batches `batch_id`, `status`, `progress.percent_complete`, `progress.succeeded_items`, items `sequence_index`, `status`, `episode_uuid`, `source_uuid`, `error`.
- **What Zep does that MiroFish relies on, beyond storage:**
  1. LLM-based entity and relationship extraction from each text episode, constrained by the registered ontology;
  2. entity resolution and deduplication across chunks;
  3. node summaries written by Zep's LLM;
  4. temporal fact handling (`valid_at`, `invalid_at`, `expired_at`);
  5. hybrid semantic plus keyword search with rerankers (`rrf`, `cross_encoder`).
- **Data sent to Zep:**
  - every 500-character chunk of the raw seed text;
  - the ontology (type names, descriptions, attribute descriptions);
  - graph names (the project name);
  - search queries, which embed entity names and report questions;
  - if graph-memory update is on, every simulated post, comment, like and follow rendered as text with agent names and quoted content.

## 7. Network hosts contacted

### Backend at runtime

| Host | When | Carries seed-derived data? |
|---|---|---|
| `LLM_BASE_URL` host (default DashScope `dashscope.aliyuncs.com`, code fallback `api.openai.com`) | every LLM call | **Yes** |
| `LLM_BOOST_BASE_URL` host | Reddit simulation, only if `LLM_BOOST_API_KEY` is set | **Yes** |
| `api.getzep.com` | graph build, persona prep, simulation memory update, report | **Yes** |
| `huggingface.co` (and its CDN hosts) | first Twitter recommendation refresh downloads `Twitter/twhin-bert-base`. A failed download is caught and only logged; the Twitter feed is then not re-ranked. | No (model download) |
| `openaipublic.blob.core.windows.net` | first use of CAMEL's tiktoken token counter downloads the `o200k_base`/`cl100k_base` encoding | No (file download) |
| AgentOps / Langfuse / Traceroot endpoints | only if their env vars are set | Yes, if enabled |

No other HTTP clients exist in `backend/app` or `backend/scripts` (searched for `requests`, `urllib`, `posthog`, `sentry`, `langsmith`, `wandb`). `httpx` is used only for Zep error types.

### Frontend (in the browser)

- `fonts.googleapis.com` and `fonts.gstatic.com`: Google Fonts stylesheet linked from `frontend/index.html`. Leaks the visitor's IP and referrer, not seed content.
- `github.com`: only if the GitHub link on the home page is clicked.
- No analytics or telemetry scripts found in `frontend/src`.
- Vite is started with `open: true`, so it opens a browser tab automatically.

### Install time

npm registry, PyPI (via uv; includes the large `torch` wheel), `ghcr.io` (Docker image), `github.com`.

## 8. License

- MiroFish is **AGPL-3.0** (`LICENSE`, `package.json`, `backend/pyproject.toml`). Its dependencies OASIS and CAMEL-AI are separate projects with their own licenses (not reviewed here).
- **Running it privately, modified or not:** allowed. The GPL family puts no obligation on private use or private modification.
- **Obligations are triggered by:**
  1. *Conveying* the software, i.e. giving a copy to someone else. You must then provide the corresponding source of your version under AGPL-3.0;
  2. AGPL section 13: if you **modify** it and let other people interact with it **over a network** (for example, hosting it for colleagues), you must offer those users the corresponding source of your modified version.
- **For this project:**
  - our patches in `ops/patches/` are modifications of MiroFish and are AGPL-3.0 if distributed;
  - our wrapper scripts talk to MiroFish only over its HTTP API and its output files, and do not import its code, which normally makes them separate works;
  - binding everything to `127.0.0.1` for single-user use keeps us clear of section 13.
- This is a plain-language reading, not legal advice.

## 9. REST API (for scripted runs)

Every response is `{"success": bool, "data": {...}, "error": "..."}`. Send `Accept-Language: en` to get English progress messages and prompts; the backend defaults to `zh` (`backend/app/utils/locale.py:30`).

**Ordered sequence for one run**

1. `POST /api/graph/ontology/generate`
   - multipart fields: `files` (repeatable), `simulation_requirement` (the question text), optional `project_name`, `additional_context`;
   - synchronous LLM call, so allow a long timeout;
   - returns `project_id`.
2. `POST /api/graph/build`
   - JSON fields: `project_id`, optional `chunk_size`, `chunk_overlap`, `force`;
   - returns `task_id`. Poll `GET /api/graph/task/<task_id>` until `status` is `completed` or `failed`;
   - `result` holds `graph_id`, `node_count`, `edge_count`, `chunk_count`.
3. `POST /api/simulation/create`
   - JSON fields: `project_id`, `graph_id`, `enable_twitter`, `enable_reddit`;
   - returns `simulation_id`.
4. `POST /api/simulation/prepare`
   - JSON fields: `simulation_id`, optional `entity_types`, `use_llm_for_profiles` (true), `parallel_profile_count` (5), `force_regenerate`;
   - returns `task_id` and `expected_entities_count` (the agent count, known **before** personas are generated);
   - poll `POST /api/simulation/prepare/status`.
5. `POST /api/simulation/start`
   - JSON fields: `simulation_id`, `platform` (`parallel`, `twitter` or `reddit`), `max_rounds`, `enable_graph_memory_update`, `force`;
   - poll `GET /api/simulation/<id>/run-status`. When `twitter_completed` and `reddit_completed` are both true, the rounds are done.
6. Optional interviews (`/api/simulation/interview/batch`) while the environment is alive. Then `POST /api/simulation/close-env` and poll until `runner_status` is `completed`.
7. `POST /api/report/generate`
   - JSON fields: `simulation_id`, `force_regenerate`;
   - returns `report_id` and `task_id`. Poll `POST /api/report/generate/status`, then `GET /api/report/<report_id>` (`markdown_content`).
8. `POST /api/report/chat` for follow-up questions.

**Deletion:** `DELETE /api/graph/project/<project_id>` removes the project directory and the graph; `DELETE /api/report/<report_id>`; there is no simulation-delete route.

## 10. Security observations (to address in Phase 3)

1. Backend binds `0.0.0.0` by default (`FLASK_HOST`). Fix by setting `FLASK_HOST=127.0.0.1`; no patch needed.
2. Frontend runs `vite --host`, which binds all interfaces, and the vite config has no `host` override. Needs a patch (or running `vite` directly with `--host 127.0.0.1`).
3. Docker compose publishes `3000:3000` and `5001:5001` on all host interfaces, and the container itself must bind `0.0.0.0` internally. Fix with `127.0.0.1:` port prefixes in an override.
4. CORS allows `*` on `/api/*`. With a localhost-only bind, any web page open in the same browser could still call the API (CSRF-style). Patch to restrict origins to `http://127.0.0.1:3000` and `http://localhost:3000`.
5. Google Fonts loaded from `index.html`. Patch it out or self-host.
6. Full JSON request bodies are logged at DEBUG level (`app/__init__.py:56-57`), and the logger and its file handler default to DEBUG (`utils/logger.py:30,74`). So chat messages, interview prompts and other JSON payloads are written to `backend/logs/*.log` **by default**. The multipart seed upload itself is not JSON and is not logged this way. `purge.sh` must cover `backend/logs/`, and a patch or log-level override should be considered.
7. Simulation subprocess inherits all secrets in its environment.
8. Seed text is fed verbatim into ontology, extraction, persona and report prompts. There is no separation of instructions from data, so instruction-injection in a seed is plausible. To be tested in Phase 3.

**Phase 3 status (2026-11-14):**
1. Done: `.env` sets `FLASK_HOST=127.0.0.1`; `start.sh` refuses any other value and checks the listening sockets after startup.
2. Done without a patch: `start.sh` runs `vite --host 127.0.0.1 --strictPort` directly, with `BROWSER=none`.
3. Done in `ops/docker/compose.local.yml` (internal network, loopback-only published ports, built from the pinned source). Untested: Docker is not installed here.
4. Done: patch 0002 (`MIROFISH_CORS_ORIGINS`, plus a `Host` allow-list against DNS rebinding). Verified live: a foreign `Origin` gets no CORS header and a foreign `Host` gets 403.
5. Done: patch 0002 removes the Google Fonts links.
6. Partly addressed: `MIROFISH_LOG_REQUEST_BODIES=false` (patch 0002) stops request-body logging. Other DEBUG lines (entity names, progress messages) still reach `backend/logs/`, so `purge.sh --logs` shreds all logs.
7. Accepted: the only secrets in the environment are a dummy `LLM_API_KEY` and Flask's `SECRET_KEY`. `start.sh` removes tracing, telemetry and proxy variables first.
8. Tested: see `ops/INJECTION_TEST.md`. Embedded commands were not obeyed, but injected assertions became graph facts, and the report described an injected "actor" as if it had posted.

## 11. Implications for the wrapper (Phases 2 and 4)

- **Rounds:** set `max_rounds` on `/start`. `OASIS_DEFAULT_MAX_ROUNDS` does nothing. Because `max_rounds` only truncates, the planner's value is an upper bound; the actual rounds may be fewer if the LLM's time config yields fewer.
- **Agent cap:** not exposed. Read `expected_entities_count` from `/prepare` (or count typed nodes after graph build) and warn or abort above the configured cap. Use `entity_types` to drop low-value types, and trim the seed.
- **Platform choice:** `platform=reddit` or `twitter` instead of `parallel` would halve the simulation calls, but it does not work through the API: upstream's single-platform scripts never write `<platform>/actions.jsonl` or its `simulation_end` event, so the backend keeps reporting round 0 after the simulation has finished (found in the Phase 4 pilot, 2026-11-14). The wrapper therefore only drives `parallel`.
- **Simulated clock:** round r is hour `(r * minutes_per_round // 60) % 24`, starting at 00:00 (`run_parallel_simulation.py:1235-1236`), and an agent can only be selected when that hour is in its generated `active_hours`. The Phase 4 pilot (10 rounds, 60 min per round) therefore covered 00:00-09:00: rounds 1-8 had no eligible agent, and only 5 agent turns happened in the whole run. The hour is used only for agent selection and log labels, never shown to agents, so the wrapper shifts every hour list in the generated `simulation_config.json` by `clock_start_hour` (08:00) after prepare and before `/start`, which reads the file at start (`simulation_runner.py:393-400`). This is a change to generated data, not to MiroFish code; it is marked with `ops_clock_start_hour` in the config and recorded in the run manifest (`schedule` stage, with config hashes before and after).
- **Action-log quirk:** the parallel runner reads new actions from each platform's `trace` table starting at rowid 0, so the first round with any active agent re-logs round 0's initial posts in `<platform>/actions.jsonl`. The SQLite tables (what `transcript.json` is exported from) are not affected; don't count agent turns from `actions.jsonl`.
- **Graph-memory update:** pass `enable_graph_memory_update=false` to stop simulated posts being pushed into the memory layer. This cuts traffic and cost, but the ReportAgent then sees only the seed graph plus the SQLite and JSONL transcript.
- **Completion:** the wrapper must call `/close-env` after rounds finish, or the run never reaches `completed` and the report cannot start.
- **Local LLM:** `LLM_API_KEY` must be non-empty. All stages use one model. JSON mode (`response_format={"type":"json_object"}`) is requested and falls back to prompt-only JSON if the server rejects it. `<think>` blocks are stripped from responses, so Qwen3 thinking output is tolerated, but it costs time and tokens.
