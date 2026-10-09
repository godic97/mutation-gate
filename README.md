# mutation-gate

A Claude Code plugin that does not take Claude's word that its tests are good. When Claude tries to end a turn, a `Stop` hook runs mutation testing on the lines Claude changed. If the tests do not kill enough mutants, the hook blocks the stop and hands Claude the list of surviving mutants to fix.

The verdict comes from the harness, not from the model. Claude cannot skip it, and a `PreToolUse` guard denies the usual ways around it.

- JavaScript / TypeScript with vitest: [StrykerJS](https://stryker-mutator.io/) (`@stryker-mutator/vitest-runner`)
- Python with pytest: [mutmut](https://github.com/boxed/mutmut) 3.x

## How it works

```
SessionStart      record HEAD of the project repo (the base)
PreToolUse        record HEAD of every other repo Claude edits (Edit/Write path, Bash cwd, cd dir, git -C dir)
                  deny edits to the gate itself, suppression comments and mutation-config changes
Stop              diff each repo against its base -> changed source lines
                  Stryker --mutate file:start-end   /   mutmut run <touched functions>
                  keep mutants on changed lines only, drop mutants Max allowed
                  score = killed / (killed + survived) >= threshold ? pass : block
UserPromptSubmit  reset the block counter
```

- **Only changed lines count.** Weak tests for old code are not Claude's problem this turn.
- **Block, then give up loudly.** A failing gate blocks the stop up to `max_blocks` times (default 3) per user prompt. After that the turn may end, and Max gets a `게이트 실패` banner with the number of unresolved mutants.
- **Tool problems never block.** If Stryker or mutmut is missing, crashes or runs out of time, the turn ends and Max sees the error text.
- **Failing or missing tests do block.** A failing test suite, or changed code that no test imports, counts as a failure.
- **Results are cached** per session by a fingerprint of the diff, the threshold and the allowlist. A turn that changes nothing does not rerun mutation testing. Timeouts are cached too; a missing tool is not, so installing it takes effect on the next stop.
- **One run per repo at a time.** Sessions that share a repo wait on a per-repo lock instead of racing over `mutants/` or the Stryker work dir.
- **Nothing to mutate is reported.** When the changed lines hold no mutants (comments, declarations, module-level Python), the banner says so instead of showing a bare 100%.

## Guard rules (PreToolUse)

Denied for Claude, in the main thread and in subagents:

- Editing anything under a `mutation-gate` directory, `$CLAUDE_PLUGIN_ROOT`, `$CLAUDE_PLUGIN_DATA` or the config dir. This includes the plugin's source checkout: to let Claude work on the plugin itself, disable it first.
- Bash commands that write (`>`, `sed`, `cp`, `mv`, `python -c`, ...) and point at the gate (`$CLAUDE_PLUGIN_ROOT`, `$MUTATION_GATE_HOME`, `mutation-gate`, `mutation_gate`).
- Changing `mutation-gate` entries, `disableAllHooks` or `allowManagedHooksOnly` in `.claude/settings*.json`, or writing settings files from Bash.
- Adding `Stryker disable` or `pragma: no mutate` comments to JS/TS or Python source.
- Adding mutmut scope keys (`do_not_mutate`, `only_mutate`, `source_paths`, ...) to `pyproject.toml`, `setup.cfg` or `mutmut.toml`.
- Running the CLI's `allow`, `disallow`, `on`, `off`, `threshold` or `hook`, and `claude plugin disable|uninstall`. Claude may run `mutation-gate status` and `mutation-gate last`.

The `Stop` hook also scans the diff for suppression comments, so a marker added through a route the guard missed still fails the gate. It also fails the gate when the plugin's own files change during the session.

This is a guard against a lazy or over-eager model, not against an adversary. A determined process with shell access can still get around it, for example by stashing its changes before stopping, or by editing a plugin file through an obfuscated path and reverting it before the turn ends.

## Install

Requirements: `python3` 3.10+ on `PATH`, and git.

```
/plugin marketplace add godic97/mutation-gate
/plugin install mutation-gate@mutation-gate
```

For local development: `claude --plugin-dir /path/to/mutation-gate`.

Put the CLI on your `PATH` so you can run it with the `!` prefix:

```
ln -s /path/to/mutation-gate/bin/mutation-gate ~/.local/bin/mutation-gate
```

Each project needs its own mutation tool. The gate tells you the exact command when one is missing:

- JS/TS: `pnpm add -D @stryker-mutator/core @stryker-mutator/vitest-runner` (or npm/yarn). Stryker runs from the git root and uses the project's own vitest config.
- Python: a `.venv` (or `venv`) at the git root with `pytest` and `mutmut` installed, for example `uv pip install --python .venv/bin/python mutmut`.

## Commands

Run these yourself, for example as `! mutation-gate allow 1a2b3c4d "same result at the boundary"` in Claude Code. The guard denies the commands that change the gate to Claude.

| Command | Effect |
|---|---|
| `mutation-gate status` | Threshold, limits, and whether the current project is on; lists allowed mutants (Claude may run this) |
| `mutation-gate last` | Details of the last verdict for the current project (Claude may run this) |
| `mutation-gate allow <id> [reason]` | Accept an equivalent mutant (one no test can kill) for the current project |
| `mutation-gate disallow <id>` | Remove that exception |
| `mutation-gate off` / `on` | Turn the gate off or on for the current project |
| `mutation-gate threshold <0-100>` | Set the passing score (default 80) |

Mutant ids hash the file, mutator, original text and replacement, so they stay the same when lines move.

Config, the allowlist and session state live in `~/.config/mutation-gate/` (override with `MUTATION_GATE_HOME`). `config.json` also holds `max_blocks` (default 3) and `budget_seconds` (default 480; the Stop hook timeout is 600).

## Limits

- Stryker runs from the git root. Monorepos whose packages carry their own Stryker install are not supported yet.
- Stryker copies the project into a sandbox for every run. Large repos pay that cost on each verdict that is not cached.
- mutmut 3.x mutates only functions and methods of top-level classes. Changes to module-level code, nested functions and functions with unknown decorators produce no mutants.
- mutmut's `mutants/` directory is created at the git root during a run and removed afterwards. An existing `mutants/` directory not created by the gate is left alone, and the gate reports an error.

## Development

```
./tests/setup_fixtures.sh            # Stryker + vitest and mutmut + pytest for the integration tests
uv venv .venv && uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```
