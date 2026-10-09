---
name: mutation-test
description: Mutation-test the code under test after the normal test run, to show the tests actually catch bugs. Use whenever the user asks to test code, run the tests, write or add tests, check test coverage or test quality ("테스트해", "테스트 돌려", "테스트 짜줘"), and before saying any tests are adequate.
---

# Mutation testing with mutation-gate

A green test run proves little: a test that asserts nothing passes too. After the normal tests pass, check that they fail when the code is broken on purpose.

## 1. Run the project's normal tests first

Fix failures before going on; mutation testing on a failing suite is meaningless.

## 2. Mutation-test the source under test, not the test files

```
mutation-gate test <source files or directories>
```

Run it as a command of its own: no `command -v` check, no pipes, no `&&`, so a permission rule such as `Bash(mutation-gate test:*)` covers it. Without arguments it tests the uncommitted source files. Give the Bash tool a timeout of 600000, because a run can take minutes. Only if the shell reports `mutation-gate: command not found`, run `"${CLAUDE_PLUGIN_ROOT}/bin/mutation-gate" test …` instead.

It uses a real mutation tool per language: Stryker (JS/TS with vitest or jest), mutmut (Python), cargo-mutants (Rust), a Go mutation tool, PIT (Java/Kotlin with Maven), Stryker.NET (C#) and Stryker4s (Scala).

**If it says the tool is not installed**, ask the user once whether to install it, quoting the exact install command from the output (use AskUserQuestion when you have it). If they agree, run that command, then run `mutation-gate test` again. If they decline, use LLM mode (section 4) instead.

**If it says there is no mutation adapter for the language**, use LLM mode (section 4).

## 3. Kill the survivors with real assertions

Each surviving mutant is a code change the tests did not notice, shown as `file:line [mutator] original → replacement (id …)`.

For each one, add or tighten an assertion that fails on that change: boundary values, exact return values, branch outcomes, error paths. Test behaviour, not implementation details. In Python, import the code by its package name (`from pkg.mod import f`), not through `src.`: mutmut cannot trace `src.` imports.

Rerun the same command until it prints `PASS`, or until only survivors remain that no test can kill (equivalent mutants, such as `x < lo` → `x <= lo` in a clamp that returns `lo` either way).

## 4. LLM mode: write the mutants yourself

For languages without an adapter, or when the user does not want the tool installed. You choose the mutations; mutation-gate applies each one, runs the project's tests, and restores the file byte for byte.

1. Pick the code that matters: permissions and auth, money, deletion, validation, limits and expiry, and functions with real branching that have a nearby test file.
2. Write 8–15 mutations to a JSON file **outside the repo** (for example in `$TMPDIR`):

   ```json
   {"mutations": [{
     "file": "src/auth/permissions.rb",
     "find": "user.id == post.owner_id",
     "replace": "true",
     "symbol": "can_edit?",
     "consequence": "can_edit? ignores the owner",
     "breaksOn": "can_edit?(user_b, post_of_a) returns true"
   }]}
   ```

   `find` must occur exactly once in the file; include surrounding text to make it unique. Most mutations should be mechanical: flip a boundary (`<`/`<=`), negate a condition, return `true`/`false` from a predicate, drop a `?? default` / `|| default` fallback, swap arithmetic, return an empty value (`nil`, `[]`, `0`, `""`), turn a `throw`/`raise` into a no-op, make a guard clause never fire, shift an index by one. Add two or three plausible real bugs too: checking the wrong user's id, applying a discount before tax instead of after, returning the unfiltered list.
3. Run it:

   ```
   mutation-gate mutate <manifest.json>
   ```

   The test command is detected from the project (`cargo test`, `go test ./...`, `mvn -q test`, `dotnet test`, `sbt test`, `npm test`, pytest, `bundle exec rspec`, `vendor/bin/phpunit`, …). Pass `--test-cmd "<command>"` when that is wrong. Never put a command in the manifest. If a run was interrupted, `mutation-gate restore` puts the files back.
4. **Only report a survivor with a concrete input that gives a wrong result.** If you cannot name one, the mutation does not change behaviour: drop it.

## 5. Check new tests against siblings (optional, recommended in LLM mode)

A test written while looking at one mutant can fit that mutant and nothing else. Write the test from the function's contract, not from the mutation, then check it:

```json
{"mutation": {"file": "...", "find": "...", "replace": "..."},
 "siblings": [{"file": "...", "find": "...", "replace": "..."}, {"file": "...", "find": "...", "replace": "..."}]}
```

```
mutation-gate verify <spec.json> --test-cmd "<command that runs only the new test>"
```

Siblings are 2–3 other mutations of the same function (a different boundary, a different branch). The test is accepted only if it passes on clean code, fails on the mutation, and kills at least one sibling. Exit 0 = accepted, 1 = rejected (rewrite the test against the contract), 2 = bad spec (fix the spec, not the test).

## 6. Report with evidence

Quote the final summary line of the command output verbatim. List any remaining survivors with their ids (and, in LLM mode, the input that breaks) and say why no test can kill them. Leave accepting them to the user (`mutation-gate allow <id> <reason>`); do not run that yourself.

Never:
- add `// Stryker disable`, `# pragma: no mutate` or `#[mutants::skip]`, or change mutation-tool settings;
- skip, delete or weaken tests to change the score;
- state a score without having run the command in this turn.
