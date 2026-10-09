# mutation-gate

Mutation testing for Claude Code. When Claude tests your code, it also checks that the tests would catch bugs.

Passing tests prove little on their own: a test that asserts nothing passes too. Mutation testing changes the code slightly, one small bug at a time (`<` → `<=`, `+` → `-`, `18` → `19`, a condition forced to `true`, an emptied block, ...), and runs the tests against each change. A **killed** mutant made some test fail. A **surviving** mutant is a bug the tests would not notice. The score is killed / (killed + survived).

Supported stacks:

- JavaScript / TypeScript (also `.vue`, `.svelte`) with vitest: [StrykerJS](https://stryker-mutator.io/) with `@stryker-mutator/vitest-runner`
- Python with pytest: [mutmut](https://github.com/boxed/mutmut) 3.x

## What it does

- **Whenever Claude writes or runs tests.** Ask "test this", "write tests for X" or "테스트해", or let Claude add tests on its own: when it edits a test file or runs a test command (`pytest`, `vitest`, `npm test`, ...), a `PostToolUse` hook reminds it, once per prompt, to mutation-test the code under test. The bundled `mutation-test` skill then has Claude run `mutation-gate test <files>`, add assertions until the surviving mutants die, rerun, and quote the final result line. You can also start it with `/mutation-gate:mutation-test`.
- **At the end of each turn, if you turn it on for a repo.** After `mutation-gate on`, a `Stop` hook mutation-tests the lines Claude changed in that repo during the session and shows the result in a banner: `✓` with the score, or `✗` with the surviving mutants. It only reports; it never stops Claude.

## Quick start

```
/plugin marketplace add godic97/mutation-gate
/plugin install mutation-gate@mutation-gate
```

Install the mutation tool in each project you want to test:

```
pnpm add -D @stryker-mutator/core @stryker-mutator/vitest-runner      # JS/TS (or npm/yarn)
uv pip install --python .venv/bin/python mutmut                        # Python, into the repo's .venv
```

Then ask Claude to test something. To also get a report at the end of every turn in a repo, run `! mutation-gate on` there.

To let Claude run mutation tests without a permission prompt each time, allow the command in `.claude/settings.json` (or `~/.claude/settings.json`):

```json
{ "permissions": { "allow": ["Bash(mutation-gate test:*)"] } }
```

The plugin's `bin/` directory is on the Bash tool's `PATH` while the plugin is enabled, so `mutation-gate` works as a bare command, including with the `!` prefix. For local development, load the checkout with `claude --plugin-dir /path/to/mutation-gate`.

Requirements: `python3` 3.11 or later on `PATH`, and git.

## `mutation-gate test`

```
mutation-gate test [paths...] [--budget SECONDS]
```

Mutation-tests whole source files: every source file under the given paths, or the uncommitted source files when no path is given. Test files, configs and type declarations are skipped. It prints the score, every surviving mutant as `file:line [mutator] original → replacement (id …)`, and `PASS` or `FAIL` against the threshold (default 80%). It exits 0 on pass, 1 on fail and 2 when there is nothing to test or the tool could not run.

## End-of-turn report

The report is off in every repo until you run `mutation-gate on` there, because it runs the repo's tests on every turn.

- **Only this session's changes count.** When the session starts, or when Claude first touches the repo, the plugin takes a snapshot: a git tree of the repo's code files as they are on disk, built from a private copy of the index. Your index, refs and stash are not touched. Work that existed before the session is in the snapshot, so it is not counted. Commits Claude makes during the session do not hide anything.
- **Changed lines only.** Stryker runs on the changed files and mutmut on the changed functions. Only mutants that overlap changed lines count.
- **Notes next to the score.** The banner also points out things that change what the score means: suppression comments (`Stryker disable`, `pragma: no mutate`) added, tests skipped or focused (`.skip`, `.only`, `@pytest.mark.skip`, ...), mutmut settings changed, test files deleted, `git stash` used, and changed code that has no mutants at all.
- **Cached.** A turn with no new changes does not rerun anything. One run per repo at a time; a second session waits up to 60 s.

## Commands

| Command | Effect |
|---|---|
| `mutation-gate test [paths...]` | Mutation-test whole files now |
| `mutation-gate on` / `off` | Turn the end-of-turn report on or off for the current repo; `on` also lists the installed mutation tools |
| `mutation-gate status` | Threshold, budget, whether the repo is on, accepted mutants |
| `mutation-gate last` | Full details of the last result for the current repo |
| `mutation-gate allow <id> [reason]` | Accept an equivalent mutant (one no test can kill) so it no longer counts |
| `mutation-gate disallow <id>` | Count it again |
| `mutation-gate threshold <0-100>` | Set the passing score (default 80) |

An equivalent mutant changes the code without changing its behaviour, for example `x < lo` → `x <= lo` in a clamp that returns `lo` either way. Accept those with `allow`. Mutant ids hash the file, the mutated text and its replacement, the enclosing function or the full multi-line span, and an occurrence index, so they stay the same when lines move.

## What the plugin runs and writes

- **Runs:** `git` (read-only on your repo, apart from tree objects in `.git/objects`; the temporary index lives in the system temp directory); the project's own `node_modules/.bin/stryker` or `.venv/bin/mutmut`, which run the project's tests; and `python -m pytest` from the repo's venv when mutmut's sandbox fails, to tell a sandbox problem from a failing suite. It refuses a tool binary that git tracks. It makes no network requests itself.
- **Writes in the repo, during a run only:** `.stryker-tmp/` (Stryker's sandbox, deleted by Stryker) and `mutants/` (mutmut's sandbox, deleted by the plugin). It deletes `mutants/` only when it created it in that run, checked with a random token. A `mutants/` it did not create is left alone and reported.
- **Writes outside the repo:** `~/.config/mutation-gate/` (override with `MUTATION_GATE_HOME`), mode 0700. It holds `config.json`, `allow.json`, per-session state with cached results (which include short snippets of mutated source), locks, and the Stryker config of the last run. The Stryker report is deleted once read. Session files older than 14 days are pruned.

## Configuration

`~/.config/mutation-gate/config.json`:

| Key | Default | Range |
|---|---|---|
| `threshold` | 80 | 0–100 |
| `budget_seconds` | 480 | 60–540 (the Stop hook times out at 600) |
| `enabled` | `[]` | repo roots, set with `on` / `off` |

## Limits

- Stryker runs per vitest project: the nearest directory with a `vitest.config.*` or `vite.config.*`, else the repo root. jest and other runners are not supported.
- mutmut 3 mutates only top-level functions and methods of top-level classes without decorators (a lone `@staticmethod` or `@classmethod` is fine). Changes elsewhere are reported as not verified.
- mutmut copies only the source and test directories into its sandbox. Tests that read other files fail there, and the plugin reports a sandbox error. Fix it with `also_copy` in `[tool.mutmut]`.
- Lines pulled in by `git pull` during a session count as changed.

## Development

```
./tests/setup_fixtures.sh            # Stryker + vitest and mutmut + pytest for the integration tests
uv venv .venv && uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```
