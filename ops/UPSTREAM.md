# Upstream pin

| Field | Value |
|---|---|
| Repository | https://github.com/666ghj/MiroFish |
| Local path | `./mirofish` |
| Pinned commit | `7657031ac01184afe2cb220f5ee3545573b5e843` |
| Commit date | 2026-10-02T02:20:36+08:00 |
| Commit subject | `chore: update Star History (#832)` |
| Cloned on | 2026-11-14 |
| License | AGPL-3.0 (see `mirofish/LICENSE`, and `ops/NOTES.md` section 8) |

## Reproduce the checkout

```bash
git clone https://github.com/666ghj/MiroFish.git mirofish
git -C mirofish checkout 7657031ac01184afe2cb220f5ee3545573b5e843
```

## Verify the checkout is unmodified

```bash
git -C mirofish rev-parse HEAD          # must print the pinned commit
git -C mirofish status --porcelain      # must print nothing before patches are applied
```

## Patches

All changes to upstream live in `ops/patches/*.patch`, applied in filename order by:

```bash
ops/scripts/apply_patches.sh            # apply (skips patches already applied)
ops/scripts/apply_patches.sh --check    # report state only
ops/scripts/apply_patches.sh --revert   # back to pristine upstream code
```

The script refuses to run unless `mirofish` is at the pinned commit. `--revert` only reverses our patches; runtime data under `mirofish/backend/uploads` (git-ignored upstream) is untouched.

| Patch | Purpose | Upstream files touched | New files |
|---|---|---|---|
| `0001-local-memory-backend.patch` | Option (a) of `ZEP_DECISION.md`: local graph memory selected by `MIROFISH_MEMORY_BACKEND=local` | `backend/app/config.py` (backend switch, placeholder key; in local mode, validation that `LLM_BASE_URL` and any `LLM_BOOST_BASE_URL` are loopback), `backend/app/utils/zep.py` (factory switch; ingestion wait raised from 600 s to 7,200 s in local mode) | `backend/app/local_graph/` (9 modules), `backend/tests/test_local_graph.py` (24 tests), `backend/tests/conftest.py` (stops tests reading `mirofish/.env`, so a local setup cannot change what upstream's Zep tests exercise) |
| `0002-local-hardening.patch` | Phase 3 hardening; every setting defaults to upstream behaviour | `backend/app/__init__.py` (`MIROFISH_CORS_ORIGINS` restricts CORS origins; `MIROFISH_ALLOWED_HOSTS` answers 403 to any other `Host` header, which blocks DNS rebinding; `MIROFISH_LOG_REQUEST_BODIES=false` stops JSON request bodies being written to the DEBUG log), `frontend/index.html` (Google Fonts links removed; every font stack has a generic fallback) | `backend/tests/test_local_hardening.py` (4 tests) |

Verified on 2026-11-14: both patches apply in order to a pristine `git archive` of the pinned commit, and the backend suite passes there (158 tests: 130 upstream + 24 adapter + 4 hardening). With the new variables unset, upstream behaviour (Zep Cloud, open CORS, no host check, body logging) is unchanged.

To regenerate a patch after editing inside `mirofish/`, diff only that patch's paths. The two patches touch disjoint files, so either can be regenerated with both applied:

```bash
# 0001
git -C mirofish add -N backend/app/local_graph backend/tests/test_local_graph.py backend/tests/conftest.py
git -C mirofish diff --binary HEAD -- backend/app/config.py backend/app/utils/zep.py backend/app/local_graph \
    backend/tests/test_local_graph.py backend/tests/conftest.py ':(exclude)backend/**/__pycache__/**' \
    > ops/patches/0001-local-memory-backend.patch
git -C mirofish reset -q

# 0002
git -C mirofish add -N backend/tests/test_local_hardening.py
git -C mirofish diff --binary HEAD -- backend/app/__init__.py frontend/index.html backend/tests/test_local_hardening.py \
    > ops/patches/0002-local-hardening.patch
git -C mirofish reset -q
```

## Runtime shim (not a patch)

`ops/runtime/sitecustomize.py` is put on `PYTHONPATH` by `ops/scripts/start.sh`; it changes no file in `mirofish/` or in the locked dependencies. It works around a bug in the locked `transformers 4.57.3`: tokenizers with more than 100k tokens trigger a Mistral-regex check that calls `huggingface_hub.model_info()` even with `HF_HUB_OFFLINE=1`, so OASIS's Twitter recommender (`Twitter/twhin-bert-base`) cannot load offline. When offline mode makes that lookup fail, the shim returns the tokenizer unchanged, which is correct for this BERT model. The same file hosts the network-audit hook used by `netcheck.sh`.

## Dependency pins (from upstream lockfiles, not upgraded)

- Python: `backend/uv.lock` (requires-python `>=3.11,<3.13`). Install with `uv sync --frozen`.
- Node: `package-lock.json` and `frontend/package-lock.json`. Install with `npm ci` (not `npm install`, which can rewrite the lockfile).
- Notable pinned versions: `zep-cloud 3.25.0`, `camel-oasis 0.2.5`, `camel-ai 0.2.78`, `openai 1.109.1`, `flask 3.1.2`, `torch 2.9.1`, `transformers 4.57.3`, `sentence-transformers 3.0.0`, `tiktoken 0.7.0`, `neo4j 5.23.0` (driver, transitively), `numpy 2.3.5`.

Note: upstream's own `npm run setup` uses `npm install` and `setup:backend` uses `uv sync` without `--frozen`. Our install instructions use `npm ci` and `uv sync --frozen` instead so the lockfiles are honoured exactly.

## Reviewed but not used

| Repository | Commit reviewed | Local path | Status |
|---|---|---|---|
| https://github.com/nikmcfly/MiroFish-Offline | `313fe642853ff9fff05e3ecae2e439886c2d29f4` (2026-03-24) | `./_review/MiroFish-Offline` | Read only, never executed. See `ops/ZEP_DECISION.md`. |
