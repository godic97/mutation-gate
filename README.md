# mutation-gate

A Claude Code plugin that does not take Claude's word that its tests are good. It mutation-tests the code: it injects small bugs (`<` → `<=`, `+` → `-`, `true` → `false`, an emptied block, ...) and checks that some test fails for each one. A mutant that survives is a bug the tests would not catch.

It works in two ways:

- **Stop gate.** In a repo you have turned on, every time Claude tries to end a turn, a `Stop` hook mutation-tests the lines Claude changed. If the tests kill too few mutants, the hook blocks the stop and hands Claude the surviving mutants to fix. The verdict comes from the hook, not from the model.
- **On demand.** When you ask Claude to test something ("test this", "테스트해"), the bundled `mutation-test` skill has Claude run the normal tests and then `mutation-gate test <files>`, strengthen the tests until the mutants die, and quote the result. You can also run `/mutation-gate:mutation-test` yourself.

Supported stacks:

- JavaScript / TypeScript (also `.vue`, `.svelte`) with vitest: [StrykerJS](https://stryker-mutator.io/) with `@stryker-mutator/vitest-runner`
- Python with pytest: [mutmut](https://github.com/boxed/mutmut) 3.x

## Quick start

```
/plugin marketplace add godic97/mutation-gate
/plugin install mutation-gate@mutation-gate
```

Then, in each repo you want gated, install the mutation tool and turn the gate on:

```
pnpm add -D @stryker-mutator/core @stryker-mutator/vitest-runner      # JS/TS (or npm/yarn)
uv pip install --python .venv/bin/python mutmut                        # Python, into the repo's .venv
! mutation-gate on
```

The plugin's `bin/` directory is on the Bash tool's `PATH` while the plugin is enabled, so `mutation-gate` works as a bare command, including with the `!` prefix. For local development, load the checkout with `claude --plugin-dir /path/to/mutation-gate`.

Requirements: `python3` 3.11 or later on `PATH`, and git.

## The Stop gate

The gate is **off in every repo until you run `mutation-gate on` there.** Running the gate means running the repo's mutation tool and its tests on every stop, which executes code from the repo. Turn it on only in repos you trust.

```
SessionStart      snapshot the project repo (if it is on)
PreToolUse        snapshot each other repo that is on, the first time Claude touches it
                  deny edits that would weaken the gate (see Guard rules)
Stop              snapshot again and diff against the first snapshot -> lines changed this session
                  Stryker on the changed files / mutmut on the changed functions
                  keep mutants that overlap changed lines, drop mutants you allowed
                  score = killed / (killed + survived) >= threshold ? pass : block
UserPromptSubmit  reset the block counter
```

- **Only this session's changes count.** A snapshot is a git tree of the repo's code files as they are on disk, built from a private copy of the index; your index, refs and stash are not touched. Uncommitted work that existed before the session is part of the first snapshot, so it is not charged to Claude. Commits Claude makes during the session do not hide anything.
- **Block, then give up loudly.** A failing gate blocks the stop up to `max_blocks` times (default 3) per user prompt. After that the turn may end, and you get a `게이트 실패` banner with the number of unresolved mutants.
- **What blocks:** a score below the threshold; failing tests; changed code that no test runs; added `Stryker disable` / `pragma: no mutate` comments; added `.skip` / `.only` / `xit` / `@pytest.mark.skip` / `pytest.xfail` in tests; changes to mutmut settings; plugin files changed during the session; a tool crash in a repo where the tool already worked this session.
- **What only warns:** the tool is missing or times out; changed code has no mutants at all (type-only edits, decorated Python functions mutmut skips); test files deleted; `git stash` used during the session; the gate's own config or allowlist changed during the session; another session is already testing the repo.
- **Results are cached** per session, keyed by the snapshot, the threshold and the allowlist. A stop with no new changes does not rerun anything.
- **One run per repo at a time.** Sessions that share a repo wait on a per-repo lock (up to 60 s).
- **Killed hooks clean up.** If Claude Code times out or interrupts the hook, it kills the tool's whole process group.

## On-demand testing

```
mutation-gate test [paths...] [--budget SECONDS]
```

Mutation-tests whole source files: every file under the given paths, or the uncommitted source files when no path is given. It prints the score, every surviving mutant with its id, and `PASS` or `FAIL` against the threshold, and exits 0 (pass), 1 (fail) or 2 (nothing to test, tool error). It works in any repo, on or off, because you asked for it. The result also shows up in `mutation-gate last`.

## Guard rules (PreToolUse)

Denied for Claude, in the main thread and in subagents:

- Editing anything under a `mutation-gate` directory, `$CLAUDE_PLUGIN_ROOT`, `$CLAUDE_PLUGIN_DATA` or the config directory, through Edit/Write tools, MCP tools that take a path, or Bash commands that write (`>`, `sed`, `cp`, `mv`, `python -c`, ...) and point there or run there. This includes the plugin's source checkout: to have Claude work on the plugin itself, disable it first.
- Changing `mutation-gate` entries, `disableAllHooks`, `allowManagedHooksOnly` or the gate's environment variables in `.claude/settings*.json`, or writing settings files from Bash.
- Adding `Stryker disable` or `pragma: no mutate` comments to source, or mutmut scope keys to `pyproject.toml`, `setup.cfg` or `mutmut.toml`.
- Running `mutation-gate allow`, `disallow`, `on`, `off`, `threshold` or `hook`, and `claude plugin disable|uninstall`. Claude may run `mutation-gate test`, `status` and `last`.

The Stop hook rescans the diff for suppression comments and skip markers, so one added through a route the guard missed still fails the gate.

This is a guard against a lazy or over-eager model, not against an adversary with shell access.

## Commands

You run the commands that change the gate yourself, for example `! mutation-gate allow 1a2b3c4d "same result at the boundary"`.

| Command | Effect |
|---|---|
| `mutation-gate on` / `off` | Turn the Stop gate on or off for the current repo; `on` also reports which mutation tools are installed |
| `mutation-gate test [paths...]` | Mutation-test whole files now (Claude may run this) |
| `mutation-gate status` | Threshold, limits, whether the repo is on, allowed mutants (Claude may run this) |
| `mutation-gate last` | Details of the last verdict for the current repo (Claude may run this) |
| `mutation-gate allow <id> [reason]` | Accept an equivalent mutant (one no test can kill) for the current repo |
| `mutation-gate disallow <id>` | Remove that exception |
| `mutation-gate threshold <0-100>` | Set the passing score (default 80) |

Mutant ids hash the file, the mutated text and replacement, the enclosing function or full multi-line span, and an occurrence index. They stay the same when lines move.

## What the plugin runs and writes

- **Runs:** `git` (read-only on your repo, plus a temporary index in the system temp directory and tree objects in `.git/objects`); the repo's own `node_modules/.bin/stryker` or `.venv/bin/mutmut`, which run the repo's tests; `python -m pytest` from the repo's venv when mutmut's sandbox fails, to tell a sandbox problem from a failing suite. It refuses a tool binary that git tracks. No network access of its own.
- **Writes in the repo, during a run only:** `.stryker-tmp/` (Stryker's sandbox, deleted by Stryker) and `mutants/` (mutmut's sandbox, deleted by the gate). The gate deletes `mutants/` only when it created it in that run, checked with a random token; a `mutants/` it did not create is left alone and reported.
- **Writes outside the repo:** `~/.config/mutation-gate/` (override with `MUTATION_GATE_HOME`), mode 0700: `config.json`, `allow.json`, per-session state with cached verdicts (which include short snippets of mutated source), locks, and the Stryker config of the last run (its report is deleted once read). Session files older than 14 days are pruned.

## Configuration

`~/.config/mutation-gate/config.json`:

| Key | Default | Range |
|---|---|---|
| `threshold` | 80 | 0–100 |
| `max_blocks` | 3 | 1–100 |
| `budget_seconds` | 480 | 60–540 (the Stop hook times out at 600) |
| `enabled` | `[]` | repo roots, set with `on` / `off` |

## Limits

- Stryker runs per vitest project: the nearest directory with a `vitest.config.*` or `vite.config.*`, else the repo root. jest and other runners are not supported.
- mutmut 3 mutates only top-level functions and methods of top-level classes without decorators (a lone `@staticmethod` or `@classmethod` is fine). Changes elsewhere get a warning, not a verdict.
- mutmut copies only the source and test directories into its sandbox. Tests that read other files fail there; the gate then reports a sandbox error instead of failing the turn. Fix it with `also_copy` in `[tool.mutmut]`.
- Lines pulled in by `git pull` during a session count as changed.
- A turn that is interrupted never reaches the Stop hook.

## Development

```
./tests/setup_fixtures.sh            # Stryker + vitest and mutmut + pytest for the integration tests
uv venv .venv && uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```
