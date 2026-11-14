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

Verified on 2026-11-14: the patch applies to a pristine `git archive` of the pinned commit, and the backend suite passes there (154 tests: 130 upstream + 24 adapter). With `MIROFISH_MEMORY_BACKEND` unset, upstream behaviour (Zep Cloud) is unchanged.

To regenerate the patch after editing the adapter inside `mirofish/`:

```bash
git -C mirofish add -N backend/app/local_graph backend/tests/test_local_graph.py backend/tests/conftest.py
git -C mirofish diff --binary HEAD -- backend ':(exclude)backend/**/__pycache__/**' > ops/patches/0001-local-memory-backend.patch
git -C mirofish reset -q
```

## Dependency pins (from upstream lockfiles, not upgraded)

- Python: `backend/uv.lock` (requires-python `>=3.11,<3.13`). Install with `uv sync --frozen`.
- Node: `package-lock.json` and `frontend/package-lock.json`. Install with `npm ci` (not `npm install`, which can rewrite the lockfile).
- Notable pinned versions: `zep-cloud 3.25.0`, `camel-oasis 0.2.5`, `camel-ai 0.2.78`, `openai 1.109.1`, `flask 3.1.2`, `torch 2.9.1`, `transformers 4.57.3`, `sentence-transformers 3.0.0`, `tiktoken 0.7.0`, `neo4j 5.23.0` (driver, transitively), `numpy 2.3.5`.

Note: upstream's own `npm run setup` uses `npm install` and `setup:backend` uses `uv sync` without `--frozen`. Our install instructions use `npm ci` and `uv sync --frozen` instead so the lockfiles are honoured exactly.

## Reviewed but not used

| Repository | Commit reviewed | Local path | Status |
|---|---|---|---|
| https://github.com/nikmcfly/MiroFish-Offline | `313fe642853ff9fff05e3ecae2e439886c2d29f4` (2026-03-24) | `./_review/MiroFish-Offline` | Read only, never executed. See `ops/ZEP_DECISION.md`. |
