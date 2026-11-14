# ops: local wrapper around upstream MiroFish

Everything we add lives here; upstream lives untouched in `../mirofish` (pinned in `UPSTREAM.md`). Changes to upstream are only ever applied from `patches/`.

> Status: Phase 2 (memory layer decided: local adapter). This README will be completed in Phase 6.
> Nothing here is validated against real outcomes, and local models weaken persona quality.

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
| `scripts/apply_patches.sh` | Apply, check or revert `patches/` against the pinned upstream commit |
| `patches/` | Re-applicable patches to upstream; listed in `UPSTREAM.md` |
| `tests/` | Wrapper tests (`python3 -m unittest discover -s ops/tests -t ops/tests`) |
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
