---
name: mutation-test
description: Mutation-test the code under test after the normal test run, to show the tests actually catch bugs. Use whenever the user asks to test code, run the tests, write or add tests, check test coverage or test quality ("테스트해", "테스트 돌려", "테스트 짜줘"), and before saying any tests are adequate.
---

# Mutation testing with mutation-gate

A green test run proves little: a test that asserts nothing passes too. After the normal tests pass, check that they fail when the code is broken on purpose.

1. **Run the project's normal tests first.** Fix failures before going on; mutation testing on a failing suite is meaningless.
2. **Run mutation testing on the source under test, not on the test files:**

   ```
   mutation-gate test <source files or directories>
   ```

   Run it as a command of its own: no `command -v` check, no pipes, no `&&`, so a permission rule such as `Bash(mutation-gate test:*)` covers it. Without arguments it tests the uncommitted source files. Give the Bash tool a timeout of 600000, because a run can take minutes. Only if the shell reports `mutation-gate: command not found`, run `"${CLAUDE_PLUGIN_ROOT}/bin/mutation-gate" test …` instead.
3. **Read the report.** Each surviving mutant is a code change the tests did not notice, shown as `file:line [mutator] original → replacement (id …)`.
4. **Kill the survivors with real assertions.** For each one, add or tighten an assertion that fails on that change: boundary values, exact return values, branch outcomes, error paths. Test behaviour, not implementation details. In Python, import the code by its package name (`from pkg.mod import f`), not through `src.`: mutmut cannot trace `src.` imports.
5. **Rerun the same command** until it prints `PASS`, or until only survivors remain that no test can kill (equivalent mutants, such as `x < lo` → `x <= lo` in a clamp that returns `lo` either way).
6. **Report with evidence.** Quote the final summary line of the command output verbatim. List any remaining survivors with their ids and say why no test can kill them. Leave accepting them to the user (`mutation-gate allow <id> <reason>`); do not run that yourself.

Never:
- add `// Stryker disable` or `# pragma: no mutate` comments, or change mutation-tool settings;
- skip, delete or weaken tests to change the score;
- state a score without having run the command in this turn.

If the command reports that Stryker or mutmut is missing, show the user the install command it printed and stop. Do not install packages without the user's go-ahead.
