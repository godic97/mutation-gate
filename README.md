# mutation-gate

Mutation testing for Claude Code. When Claude tests your code, it also checks that the tests would catch bugs.

Passing tests prove little on their own: a test that asserts nothing passes too. Mutation testing changes the code slightly, one small bug at a time (`<` → `<=`, `+` → `-`, `18` → `19`, a condition forced to `true`, an emptied block, ...), and runs the tests against each change. A **killed** mutant made some test fail. A **surviving** mutant is a bug the tests would not notice. The score is killed / (killed + survived).

## Languages

Each language uses an established mutation tool. The tools are installed per project or per user (see [Installing the tools](#installing-the-tools)); mutation-gate drives them and reads their reports.

| Language | Test setup | Mutation tool |
|---|---|---|
| JavaScript / TypeScript (also `.vue`, `.svelte`) | vitest or jest | [StrykerJS](https://stryker-mutator.io/) 10 |
| Python | pytest | [mutmut](https://github.com/boxed/mutmut) 3 |
| Rust | `cargo test` | [cargo-mutants](https://mutants.rs/) 27 |
| Go (modules) | `go test` | [gremlins](https://github.com/go-gremlins/gremlins) 0.6 |
| Java / Kotlin | Maven, JUnit 4 or JUnit 5/6 | [PIT](https://pitest.org) 1.30 |
| C# | xUnit, NUnit or MSTest (VSTest) | [Stryker.NET](https://stryker-mutator.io/docs/stryker-net/introduction/) |
| Scala | sbt 1.11.2+ or 2.x | [Stryker4s](https://stryker-mutator.io/docs/stryker4s/) 1.1 |
| Anything else (Ruby, PHP, C/C++, Gradle, ...) | the project's test command | **LLM mode**: Claude writes the mutants, mutation-gate runs them |

## What it does

- **Whenever Claude writes or runs tests.** Ask "test this", "write tests for X" or "테스트해", or let Claude add tests on its own. When it edits a test file or runs a test command (`pytest`, `vitest`, `jest`, `cargo test`, `go test`, `mvn test`, `dotnet test`, `sbt test`, `rspec`, `phpunit`, ...), a `PostToolUse` hook reminds it, once per prompt, to mutation-test the code under test. The bundled `mutation-test` skill then has Claude run `mutation-gate test <files>`, add assertions until the surviving mutants die, rerun, and quote the final result line. You can also start it with `/mutation-gate:mutation-test`.
- **When a tool is missing**, Claude asks you once whether to install it, quoting the exact command, and continues after you agree. If you decline, it uses LLM mode.
- **At the end of each turn, if you turn it on for a repo.** After `mutation-gate on`, a `Stop` hook mutation-tests the lines Claude changed in that repo during the session and shows the result in a banner: `✓` with the score, or `✗` with the surviving mutants. It only reports; it never stops Claude.

## Quick start

```
/plugin marketplace add godic97/mutation-gate
/plugin install mutation-gate@mutation-gate
```

Then ask Claude to test something. To also get a report at the end of every turn in a repo, run `! mutation-gate on` there.

To let Claude run mutation tests without a permission prompt each time, allow the commands in `.claude/settings.json` (or `~/.claude/settings.json`):

```json
{ "permissions": { "allow": ["Bash(mutation-gate test:*)", "Bash(mutation-gate mutate:*)"] } }
```

The plugin's `bin/` directory is on the Bash tool's `PATH` while the plugin is enabled, so `mutation-gate` works as a bare command, including with the `!` prefix. For local development, load the checkout with `claude --plugin-dir /path/to/mutation-gate`.

Requirements: `python3` 3.11 or later on `PATH`, and git.

## Installing the tools

mutation-gate does not install anything by itself; when a tool is missing, `mutation-gate test` prints the command and the skill asks you before running it. User-scope locations work without changing `PATH`: mutation-gate also looks in `~/.cargo/bin`, `~/.local/go/bin`, `~/go/bin`, `~/.dotnet` and `~/.dotnet/tools`, `~/.local/opt/apache-maven-*/bin`, `~/.local/opt/sbt/bin`, and for a JDK in `JAVA_HOME` or `~/.local/opt/jdk*`.

| Language | Install |
|---|---|
| JS/TS | `pnpm add -D @stryker-mutator/core @stryker-mutator/vitest-runner` (or `…/jest-runner`; npm/yarn work too) |
| Python | `uv pip install --python .venv/bin/python mutmut` (into the repo's `.venv`) |
| Rust | `cargo install --locked cargo-mutants` (Rust itself: `rustup` with `--no-modify-path` works) |
| Go | `go install github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0` |
| Java/Kotlin | a JDK 17+ and Maven 3.9 (or the project's `./mvnw`); nothing is added to the pom |
| C# | `dotnet tool install -g dotnet-stryker` (with the .NET 8 or 9 SDK add `--version 4.16.0`) |
| Scala | a JDK and sbt; Stryker4s is downloaded by sbt on the first run and nothing is added to the build |

## `mutation-gate test`

```
mutation-gate test [paths...] [--budget SECONDS]
```

Mutation-tests whole source files: every source file under the given paths, or the uncommitted source files when no path is given. Test files, configs and type declarations are skipped. It prints the score, every surviving mutant as `file:line [mutator] original → replacement (id …)`, and `PASS` or `FAIL` against the threshold (default 80%). It exits 0 on pass, 1 on fail and 2 when there is nothing to test or the tool could not run. For a language without an adapter it says so and points at LLM mode.

## LLM mode

```
mutation-gate mutate <manifest.json> [--test-cmd "<command>"] [--budget SECONDS]
mutation-gate verify <spec.json> --test-cmd "<command that runs only the new test>"
mutation-gate restore
```

For any language with a test command. Claude reads the code and writes mutations as find/replace edits, mechanical ones (flipped boundaries, negated conditions, emptied returns, removed fallbacks) and a few plausible real bugs (checking the wrong user's id, applying a discount before tax). The idea comes from [sabotage](https://github.com/malek0x1/sabotage).

```json
{"mutations": [{"file": "lib/price.rb", "find": "member && price >= 100", "replace": "member && price > 100",
                "consequence": "a 100 order gets no discount", "breaksOn": "discount(100, true) returns 100"}]}
```

- `mutate` applies each mutation in turn, runs the project's tests, and restores the file byte for byte. The test command is detected from the project (`cargo test`, `go test ./...`, `mvn -q test`, `./gradlew test`, `dotnet test`, `sbt test`, `npm test`, pytest, `bundle exec rspec`, `vendor/bin/phpunit`, `ctest`) or given with `--test-cmd`; a manifest cannot contain a command, so the command that runs is always the one you approve.
- Before mutating a file, its original bytes go to a journal in `~/.config/mutation-gate/state/journal/`. A killed run is undone at the next run, at session start, at the end of a turn, or with `mutation-gate restore`.
- `verify` checks a new test the way sabotage does: it must pass on clean code, fail on the mutation, and kill at least one sibling mutation of the same function, so a test written to fit one mutant is rejected.
- A survivor only counts in the skill's report when Claude can name an input that gives a wrong result; otherwise it is treated as an equivalent mutant.

## End-of-turn report

The report is off in every repo until you run `mutation-gate on` there, because it runs the repo's tests on every turn.

- **Only this session's changes count.** When the session starts, or when Claude first touches the repo, the plugin takes a snapshot: a git tree of the repo's code files as they are on disk, built from a private copy of the index. Your index, refs and stash are not touched. Work that existed before the session is in the snapshot, so it is not counted. Commits Claude makes during the session do not hide anything.
- **Changed lines only.** The tool runs on the changed files (or functions) and only mutants that overlap changed lines count.
- **Notes next to the score.** The banner also points out things that change what the score means: suppression comments added (`Stryker disable`, `pragma: no mutate`, `#[mutants::skip]`, `@SuppressWarnings("stryker4s.mutation…")`), tests skipped or focused (`.skip`, `.only`, `@pytest.mark.skip`, `#[ignore]`, `t.Skip`, `@Disabled`, `[Ignore]`, ...), mutation-tool settings changed, test files deleted, `git stash` used, a project's own PIT or Stryker4s settings that override mutation-gate's, and changed code that has no mutants at all.
- **Cached.** A turn with no new changes does not rerun anything. One run per repo at a time; a second session waits up to 60 s.

## Commands

| Command | Effect |
|---|---|
| `mutation-gate test [paths...]` | Mutation-test whole files now with the language's tool |
| `mutation-gate mutate <manifest>` | LLM mode: run find/replace mutations against the project's tests |
| `mutation-gate verify <spec>` | Check a new test against the mutation and its siblings |
| `mutation-gate restore` | Put back files an interrupted `mutate`/`verify` left mutated |
| `mutation-gate on` / `off` | Turn the end-of-turn report on or off for the current repo; `on` also lists the installed mutation tools |
| `mutation-gate status` | Threshold, budget, whether the repo is on, accepted mutants |
| `mutation-gate last` | Full details of the last result for the current repo |
| `mutation-gate allow <id> [reason]` | Accept an equivalent mutant so it no longer counts |
| `mutation-gate disallow <id>` | Count it again |
| `mutation-gate threshold <0-100>` | Set the passing score (default 80) |

Mutant ids hash the file, the mutated text and its replacement, the enclosing function or the full multi-line span, and an occurrence index, so they stay the same when lines move.

## What the plugin runs and writes

- **git:** read-only on your repo, apart from tree objects in `.git/objects`; the temporary index lives in the system temp directory.
- **The mutation tools**, which run the project's own tests:
  - Stryker: `node_modules/.bin/stryker`, sandbox in `.stryker-tmp/` (deleted by Stryker).
  - mutmut: `.venv/bin/mutmut`, sandbox in `mutants/` (deleted by the plugin only when it created it in that run, checked with a random token; a `mutants/` it did not create is left alone and reported). When mutmut's sandbox fails, `python -m pytest` runs once to tell a sandbox problem from a failing suite.
  - cargo-mutants: workspace copies and `mutants.out` under the work directory below; nothing in the repo. Only build settings from `.cargo/mutants.toml` are used.
  - gremlins: module copies under the work directory; a repo `.gremlins.yaml` is ignored.
  - PIT: `mvn test-compile org.pitest:pitest-maven:1.30.0:mutationCoverage` from the Maven reactor root, so Maven writes its normal `target/`; on first use Maven downloads PIT (about 5 MB) into its local repository.
  - Stryker.NET: `dotnet-stryker` from the source project's directory; builds write the usual `bin/` and `obj/`.
  - Stryker4s: `sbt --batch` with the plugin loaded from a private global plugins directory (`-Dsbt.global.plugins`); your `~/.sbt` settings stay in use and nothing is added to the build. sbt writes its normal `target/`.
  - LLM mode: the project's test command, with the mutated file restored after each run.
- It refuses a tool binary that git tracks in the repo. It makes no network requests itself; build tools may download dependencies.
- **Outside the repo:** `~/.config/mutation-gate/` (override with `MUTATION_GATE_HOME`), mode 0700. It holds `config.json`, `allow.json`, per-session state with cached results (which include short snippets of mutated source), locks, the LLM-mode journal, and per-run work directories. Tool reports are deleted once read. Session files older than 14 days are pruned.

## Configuration

`~/.config/mutation-gate/config.json`:

| Key | Default | Range |
|---|---|---|
| `threshold` | 80 | 0–100 |
| `budget_seconds` | 480 | 60–540 (the Stop hook times out at 600) |
| `enabled` | `[]` | repo roots, set with `on` / `off` |

## Limits

- **JS/TS:** Stryker runs per project: the nearest directory with a vitest, vite or jest config, else the repo root.
- **Python:** mutmut 3 mutates only top-level functions and methods of top-level classes without decorators (a lone `@staticmethod` or `@classmethod` is fine); changes elsewhere are reported as not verified. mutmut copies only the source and test directories into its sandbox; tests that read other files fail there and the plugin reports a sandbox error (fix with `also_copy` in `[tool.mutmut]`). Tests must import the code by package name, not through `src.`.
- **Rust:** only the tests of the package that holds the mutant run. Each run copies and rebuilds the workspace; heavy dependency trees use a large part of the time budget (`copy_target = true` in `.cargo/mutants.toml` helps).
- **Go:** mutants are judged by the tests of their own package; code tested only from another package shows up as not covered. Files behind build constraints for other platforms do not compile.
- **Java/Kotlin:** Maven only; Gradle projects get an error and can use LLM mode. A `pitest-maven` `<configuration>` in the pom overrides mutation-gate's settings (reported as a note). PIT mutates bytecode, so Kotlin compiler-generated code can yield mutants no test can kill; accept them with `allow`.
- **C#:** test projects need a direct `<ProjectReference>` to the changed project; VSTest only; the project's `stryker-config.json` is ignored.
- **Scala:** sbt only. Stryker4s compiles mutated classes into the project's `target/`, so the next normal compile recompiles that module; avoid running it while Metals or another sbt compiles the same project.
- **LLM mode** is only as thorough as the mutations Claude writes; it is a fallback for languages without a tool.
- Lines pulled in by `git pull` during a session count as changed.

## Development

```
./tests/setup_fixtures.sh            # tools for the integration tests; language toolchains are optional
uv venv .venv && uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```

Integration tests for a language skip when its toolchain is not installed.
