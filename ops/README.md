# ops: local wrapper around upstream MiroFish

Everything we add lives here; upstream lives untouched in `../mirofish` (pinned in `UPSTREAM.md`). Changes to upstream are only ever applied from `patches/`.

> Status: Phase 4 wrapper scripts written and tested; pilot measured (see "Batches" below). This README will be completed in Phase 6.
> Nothing here is validated against real outcomes, and local models weaken persona quality.

## Set up on a new machine

Requirements: macOS or Linux (Windows via WSL), Git, [uv](https://docs.astral.sh/uv/), Node.js 18+, [Ollama](https://ollama.com/download), and about 24 GB of GPU or unified memory for the default model (choose a smaller one in `config/model.conf` otherwise).

```bash
git clone https://github.com/sajjants/mirofish-implementation.git && cd mirofish-implementation
ops/scripts/bootstrap.sh --with-models   # clone upstream at the pin, patch, install, .env, tests, models (~19 GB), doctor
```

`bootstrap.sh` is idempotent; without `--with-models` it skips the model download. It installs only from the lockfiles (`uv sync --frozen`, `npm ci --ignore-scripts`) and never runs upstream's own setup scripts. It also caches the two files the simulation would otherwise download at runtime (about 1.1 GB, `--skip-assets` to defer) and switches `.env` to offline mode.

## Run, check, delete

```bash
ops/scripts/start.sh            # backend 127.0.0.1:5001 and UI http://127.0.0.1:3000; refuses non-loopback config
ops/scripts/stop.sh             # stops both, including simulation subprocesses (--ollama to stop Ollama too)
ops/scripts/netcheck.sh         # pilot run with network auditing: 0 PASS, 1 FAIL (violation or no evidence), 2 pilot incomplete
ops/scripts/injection_check.py RUN_ID   # did instruction text in the seed steer the run? (ops/INJECTION_TEST.md)
ops/scripts/purge.sh RUN_ID     # shred a run's project, graph, simulation, report and evidence (--logs: all logs too)
python3 ops/scripts/mirofish_api.py run --run-id ID --seed seed.md --requirement-file q.txt --max-rounds 10
```

Runs are recorded in `ops/runs/<RUN_ID>/manifest.json` (mode 0600, directory 0700). Re-running the same `RUN_ID` resumes after the last completed stage. Data directories under `ops/` are created with 0700 permissions.

Last network check (2026-11-14, full pilot on both platforms): **PASS**. Every connection from MiroFish went to 127.0.0.1 (details in `ZEP_DECISION.md` §3.1).

## Batches: seed + questions -> pilot -> confirmed runs -> checks

Seed format: lines tagged `VERIFIED:`, `ESTIMATE:`, `BELIEF:` or `UNKNOWN:` (see `examples/seed.md`); questions are `## Q1`, `## Q2`, ... sections (`examples/questions.md`). Keep real seeds in `ops/seeds/` (git-ignored).

```bash
ops/scripts/prepare_inputs.py --batch B1 --seed ops/seeds/seed.md --questions ops/seeds/questions.md \
    [--terms ops/seeds/terms.txt]          # lint, optional pseudonymisation, requirement text per question
ops/scripts/unknown_variants.py propose --batch B1   # 3-5 alternatives per UNKNOWN from the local model; edit if needed
ops/scripts/plan.py --batch B1                       # deterministic plan within config/limits.yaml (prints cuts)
ops/scripts/start.sh --no-frontend
ops/scripts/run_batch.py pilot --batch B1            # Q1, 10 rounds; prints measured calls, time, failure rate; STOPS
ops/scripts/run_batch.py run --batch B1 --confirm    # the planned runs, re-planned from the pilot's measurements
ops/scripts/factguard.py --batch B1                  # unsupported numbers/dates/entities, inactive actors, UNKNOWN stances
ops/scripts/compare.py --batch B1                    # trajectory frequencies, resistors, UNKNOWN tables, noise warnings
ops/scripts/purge.sh B1 --batch                      # shred all runs of the batch, its inputs and mapping
```

Calls are measured by `scripts/llm_meter.py`, a loopback proxy that `start.sh` puts between MiroFish and Ollama when `LLM_BASE_URL` is `http://127.0.0.1:11435/v1`. It logs per-call metadata only (time, status, tokens, whether JSON or tool-call output parsed) to `logs/llm_meter.jsonl`, never prompt or answer text. Only one batch may run at a time, and nothing else should use MiroFish meanwhile, because calls are attributed to runs by time window.

## Layout

| Path | Purpose |
|---|---|
| `UPSTREAM.md` | Pinned upstream commit, how to reproduce and verify the checkout, how to apply patches |
| `NOTES.md` | Study notes: five-stage flow, config, personas, rounds and cost, outputs, Zep calls, network hosts, license, REST API |
| `ZEP_DECISION.md` | Memory-layer options, evidence, recommendation, hostnames per path |
| `config/model.conf` | Local LLM settings (Hugging Face model ref, Ollama alias, `num_ctx`, loopback host) |
| `config/Modelfile.template` | Ollama Modelfile rendered by `build_model.sh` |
| `config/mirofish.env.example` | Template for `mirofish/.env` under the local path |
| `config/allowed_hosts.txt` | Non-loopback hosts `netcheck.sh` will tolerate (empty under the fully local path) |
| `scripts/_common.sh` | Shared shell helpers (project root, `model.conf`, Ollama binary lookup, private directories) |
| `scripts/ollama_serve.sh` | Start Ollama on loopback with cloud models disabled (`--foreground` to keep it attached) |
| `scripts/build_model.sh` | Pull the chat and embedding GGUFs through Ollama and create the aliases with a large context |
| `scripts/doctor.sh` | Check Ollama install, reachability, loopback bind, memory, model, context, JSON compliance, throughput, embeddings |
| `scripts/doctor_llm.py` | LLM checks used by `doctor.sh` (stdlib only) |
| `scripts/bootstrap.sh` | One-command, idempotent setup on a new machine (see above) |
| `scripts/apply_patches.sh` | Apply, check or revert `patches/` against the pinned upstream commit |
| `scripts/envfile.py` | Add missing keys to, set, or read `mirofish/.env` (atomic, mode 600) |
| `scripts/cache_assets.sh` | Download `Twitter/twhin-bert-base` and tiktoken encodings into `cache/`, then set `.env` offline |
| `scripts/start.sh`, `scripts/stop.sh` | Start/stop MiroFish on loopback with tracing and proxy variables scrubbed |
| `scripts/mirofish_api.py` | Stdlib REST client and resumable one-run pipeline (loopback only) |
| `scripts/netcheck.sh`, `scripts/netcheck.py` | Pilot run with audit hook and `lsof` sampling; evaluation against `config/allowed_hosts.txt` |
| `scripts/purge.sh`, `scripts/purge.py` | Overwrite-then-delete everything a run produced |
| `scripts/injection_check.py` | Measure whether seed instruction text influenced each stage |
| `config/limits.yaml` | Rounds (pilot 10, real 20-30, ceiling 40), runs per question, call/time/agent caps, cost estimates |
| `config/rubric.yaml` | Trajectory categories and the resistor definition used by `compare.py` |
| `scripts/seedfile.py` | Seed/question parsing, instruction lint, UNKNOWN variant substitution |
| `scripts/pseudonymize.py`, `scripts/depseudonymize.py` | Consistent pseudonyms for listed names, scaled figures; mapping in `mappings/` (0600) |
| `scripts/prepare_inputs.py` | Builds a batch's upload seed, requirement texts and `inputs.json`; `--submit` drives one question |
| `scripts/unknown_variants.py` | Alternatives per UNKNOWN (local LLM) and seeded, balanced variants |
| `scripts/plan.py` | Deterministic planner: cuts rounds first, then runs, and prints each cut |
| `scripts/llm_meter.py` | Loopback metering proxy in front of Ollama (metadata only) |
| `scripts/run_batch.py` | Pilot, confirmation gate, planned runs, extended manifests, transcript export, resume |
| `scripts/factguard.py` | Flags report claims not supported by the seed or the run's variant |
| `scripts/compare.py` | Classifies runs against the rubric and summarises them with noise warnings |
| `scripts/local_llm.py`, `scripts/opsconfig.py` | Shared JSON-mode client for the local model; paths and config loading |
| `examples/` | Fictional seed and questions showing the input format |
| `mappings/` | Pseudonym mappings (git-ignored, 0700) |
| `runtime/sitecustomize.py` | Offline tokenizer fix for locked `transformers`, and the network-audit hook (see `UPSTREAM.md`) |
| `docker/` | Compose override with no internet egress (untested; see `NOTES.md` §10) |
| `INJECTION_TEST.md` | Seed injection test: method, result, mitigations |
| `tests/fixtures/` | Synthetic seed (with injection canaries) and question used by `netcheck.sh` |
| `runs/`, `cache/`, `logs/` | Run manifests and evidence, offline model cache, logs (all git-ignored, 0700) |
| `patches/` | Re-applicable patches to upstream; listed in `UPSTREAM.md` |
| `tests/` | Wrapper tests (`mirofish/backend/.venv/bin/python -m unittest discover -s ops/tests`; that interpreter has PyYAML from upstream's lockfile) |
| `state/` | Machine-local generated state, e.g. `hardware_profile.json` (git-ignored) |

## Quick check of the local LLM (Phase 2)

Last result (2026-11-14, M3 Max 96 GB, Ollama 0.40.16, `mirofish-llm` = Qwen3-30B-A3B-Instruct-2507 Q4_K_M, 18.6 GB, `num_ctx` 32768): all doctor checks passed; JSON compliance 5/5; about 86 generated tokens/s, median 2.5 s per call; `mirofish-embed` returns 1024-dim vectors.

```bash
# Ollama is installed project-locally in .tools/ollama (or set OLLAMA_BIN in config/model.conf).
ops/scripts/ollama_serve.sh          # binds 127.0.0.1:11434
ops/scripts/build_model.sh           # pulls HF_MODEL_REF and EMBED_MODEL_REF from config/model.conf
ops/scripts/doctor.sh
ops/scripts/apply_patches.sh         # local memory backend
(cd mirofish/backend && UV_PYTHON=3.12 uv sync --frozen && .venv/bin/python -m pytest -q)
```
