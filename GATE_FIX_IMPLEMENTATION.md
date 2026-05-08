# Cascade Gate-Fix Loop Implementation Summary

## Overview
Implemented a comprehensive headless OpenRouter-based gate-fix loop for Cascade that automatically fixes code-related gate failures without requiring interactive UI.

## Key Features

### 1. Failure Classification
- **Deterministic Failures** (route to repair, not model):
  - Mandate metadata issues
  - Branch mismatch
  - Docker/network runtime errors
  - Workflow/environment issues

- **Model-Fixable Failures** (route to gate-fix loop):
  - Docstring/coverage issues
  - Linting/ruff errors
  - Type checking (pyright/mypy)
  - Code formatting
  - Import errors
  - Small unit test failures
  - Serializer/view mismatches

### 2. Streaming Real-Time Output
- OpenRouter API requests with streaming enabled (`stream: true`)
- Real-time token output to terminal with `[model]` prefix
- Progress tracking with `[gate-fix]`, `[apply]`, `[rerun]`, `[pass]`, `[fail]` tags
- Full artifact logging for audit trail

### 3. Model Selection Policy
- **Default (Primary)**: `deepseek/deepseek-v3.2`
  - Strong coding performance, cheap ($0.36/$1.44 per million tokens)
  - Deterministic output (temperature: 0.2)
  
- **Fallback 1**: `qwen/qwen3-coder-480b-a35b-instruct:free`
  - Absolute lowest cost (free tier)
  - Used when primary exhausted
  
- **Fallback 2**: `moonshotai/kimi-k2.6`
  - Strongest coding model
  - Used for complex fixes after free fallback

### 4. Safety Boundaries
- Max attempts cap (default: 3)
- Max estimated cost cap (default: $0.25)
- Branch drift detection (rejects if branch changes unexpectedly)
- Repeated failure detection (gives up if same signature repeats)
- Unrelated file growth detection (stops if > 5 unrelated files added)
- Worktree boundary enforcement (rejects patches outside assigned worktree)

### 5. Artifact Logging
For each run, creates:
- `gate_fix_model_call.json` - OpenRouter request metadata
- `gate_fix_attempt_N.stream.log` - Raw streamed model output
- `gate_fix_attempt_N.summary.log` - Human-readable attempt summary
- `gate_fix_attempt_N.rerun.log` - Gate re-run result after patch
- `gate_fix_prompt.md` - The prompt sent to model (if needed)
- `gate_fix_summary.json` - Final results, costs, stop reason

### 6. Patch Application
- Parses JSON from model response
- Validates all files are within assigned worktree
- Applies edits safely (no system calls)
- Verifies files after patch before rerun

## Files Changed

### New Files
- [cascade/gate_fix.py](cascade/gate_fix.py) (~800 lines)
  - Core gate-fix loop implementation
  - OpenRouter streaming integration
  - Classification and safety logic
  - Artifact logging

- [tests/test_gate_fix.py](tests/test_gate_fix.py) (~400 lines)
  - 19 comprehensive tests
  - Classification tests
  - Cost/safety tests
  - Integration tests

### Modified Files
- [cascade/commands.py](cascade/commands.py)
  - Added `gate-fix` command metadata

- [cascade/config.py](cascade/config.py)
  - Added default model profiles:
    - `deepseek/deepseek-v3.2` (primary)
    - `qwen/qwen3-coder-480b-a35b-instruct:free` (fallback)
    - `moonshotai/kimi-k2.6` (strong fallback)
  - Helper functions for model selection

- [cascade/cli.py](cascade/cli.py)
  - New CLI command: `cascade gate-fix`
  - Imports for gate_fix module and config helpers
  - ~150 lines of CLI integration

## Test Results

✅ **Total Tests**: 121 passing
- Original test suite: 102 tests
- New gate-fix tests: 19 tests

### Test Coverage
1. ✅ Deterministic failures do not enter model-fix loop
2. ✅ Docstring failures enter cheap model loop
3. ✅ Model stream output classification
4. ✅ Artifacts/logs are created
5. ✅ Repeated failures trigger stop condition
6. ✅ Cost cap enforcement
7. ✅ Unrelated file growth detection
8. ✅ Branch drift detection
9. ✅ Fallback model selection
10. ✅ Exact rerun command execution

## Usage

### For a3 Mandate Docstring Failure

**Exact Command:**
```bash
cd /Users/alfredn/Documents/instica-workspace/jungle-worktrees/a3-enrich-audit-log-messages

# Run docstring fix (default: cheap-fixer profile)
cascade gate-fix a3 --project jungle

# Or with custom parameters
cascade gate-fix a3 --project jungle --max-attempts 5 --max-cost 1.00

# With debugging
cascade gate-fix a3 --project jungle --debug-openrouter

# With specific model
cascade gate-fix a3 --project jungle --fallback-model moonshotai/kimi-k2.6
```

**Expected Output:**
```
[gate-fix] Starting gate-fix loop
[gate-fix] Model: deepseek/deepseek-v3.2
[gate-fix] Max attempts: 3
[gate-fix] Cost cap: $0.25
[gate-fix] Category: docstring

[gate-fix] ===== Attempt 1 of 3 =====
[gate-fix] Estimated cost: $0.0123
[gate-fix] Calling deepseek/deepseek-v3.2...
[model] Analyzing docstring failures...
[model] Adding missing docstrings to jungle/audit/messages.py...
[apply] ✓ Applied 1 file(s)
[apply] Changed files: jungle/audit/messages.py
[rerun] Running: make preflight
[pass] ✓ Gate passed!

✓ Gate passed!
  Attempts: 1
  Total cost: $0.0123

Summary saved: /path/to/gate_fix_summary.json
```

### For Other Code-Fixable Failures

The same command works for:
- Linting: `cascade gate-fix AGENT --project PROJECT`
- Type errors: `cascade gate-fix AGENT --project PROJECT`
- Formatting: `cascade gate-fix AGENT --project PROJECT`
- Import issues: `cascade gate-fix AGENT --project PROJECT`

### Workflow Integration

For mandatory workflows, add to closeout sequence:
```bash
# Check if gate is failing
cascade gate-status a3 --project jungle

# If fixable, run auto-fix
cascade gate-fix a3 --project jungle

# Then verify
cascade preflight a3 --project jungle

# Then stage/commit
cascade closeout a3 --project jungle --stage --commit
```

## Model Selection Logic

The gate-fix system automatically selects models:

1. **Primary**: Check `--profile` parameter
   - If not provided, default to `"cheap-fixer"`
   - If not found in config, use `deepseek/deepseek-v3.2`

2. **Fallbacks** (if primary fails):
   - Automatically tries: `qwen/qwen3-coder-480b-a35b-instruct:free`
   - Then: `moonshotai/kimi-k2.6`
   - Or use `--fallback-model` to override

3. **Cost-Aware**: Tracks total spend across attempts
   - Stops if `total_cost > max_estimated_cost_usd`
   - Default: $0.25 total
   - Can increase with `--max-cost 1.00`

## Environment Setup

### Required
```bash
export OPENROUTER_API_KEY="your-key-here"
```

### Optional
```bash
# For debug output
cascade gate-fix a3 --project jungle --debug-openrouter

# View summary
cat /path/to/state/jungle/runs/a3/gate_fix_summary.json
```

## Prompt Design

The model receives a focused, anti-looping prompt that includes:
- Repository and agent context
- Exact failing command and hook name
- Exact failing log output
- Dirty/changed files summary
- Mandate scope (files it can modify)
- Explicit instructions to:
  - Fix ONLY the specific failure
  - NOT refactor unrelated code
  - NOT weaken tests/gates
  - NOT bypass policies
  - NOT change branch
  - NOT create scratch files

## Safety Boundaries

All enforced:
1. ✅ Max attempts (default 3)
2. ✅ Cost cap (default $0.25, configurable)
3. ✅ Repeated failure detection (same signature = stop)
4. ✅ Branch drift detection (branch change = stop)
5. ✅ Unrelated file growth (> 5 new unrelated = stop)
6. ✅ Worktree boundary (reject patches outside assigned worktree)
7. ✅ Diff size check (suspicious expansion = stop)
8. ✅ Fallback exhaustion (all fallbacks failed = stop)

## Integration with Existing Cascade

Gate-fix is a **new, separate command** that:
- Does NOT interfere with existing `repair` / `loop` commands
- Routes automatically based on failure classification
- Saves artifacts separately in worktree state directory
- Can be called standalone or as part of closeout workflow
- Respects all existing mandate/worktree boundaries

## Recommended Workflow for a3

1. **Check gate status:**
   ```bash
   cascade preflight a3 --project jungle
   cascade gate-status a3 --project jungle
   ```

2. **If docstring failure detected:**
   ```bash
   cascade gate-fix a3 --project jungle
   ```

3. **On success, continue closeout:**
   ```bash
   cascade closeout a3 --project jungle --stage --commit --yes
   ```

4. **If model-fix fails, fallback to manual:**
   ```bash
   cascade logs a3 --project jungle --kind preflight
   # Fix manually
   cascade preflight a3 --project jungle
   cascade closeout a3 --project jungle --stage --commit --yes
   ```

## Exact Files and Test Counts

| Component | Lines | Status |
|-----------|-------|--------|
| gate_fix.py | ~800 | ✓ Implemented |
| test_gate_fix.py | ~400 | ✓ 19 tests passing |
| cli.py (new) | ~150 | ✓ Integrated |
| commands.py (update) | +1 line | ✓ Added command |
| config.py (update) | +40 lines | ✓ Default models |
| **Total Tests** | - | **✓ 121 passing** |

## No Breaking Changes

- All 102 existing tests still pass
- No modifications to existing repair/loop commands
- New command is additive only
- Existing mandate/closeout workflows unaffected
