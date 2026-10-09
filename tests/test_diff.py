import pytest
from conftest import git

from mutation_gate import diff


def changes(repo, base):
    return diff.changes(repo.path, base, diff.snapshot(repo.path))


# --- snapshots ------------------------------------------------------------------------------


def test_changes_report_new_and_modified_lines(repo):
    repo.write("src/a.ts", "one\ntwo\nthree\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("src/a.ts", "one\nTWO\nthree\nfour\n")

    assert changes(repo, base).added == {"src/a.ts": {2: "TWO", 4: "four"}}


def test_work_that_existed_before_the_snapshot_is_not_counted(repo):
    repo.write("src/a.py", "a = 1\n")
    repo.commit()
    repo.write("src/a.py", "a = 2\n")  # the user's uncommitted work
    repo.write("src/wip.py", "w = 1\n")  # the user's untracked work
    base = diff.snapshot(repo.path)
    repo.write("src/b.py", "b = 1\n")

    assert changes(repo, base).added == {"src/b.py": {1: "b = 1"}}


def test_changes_committed_during_the_session_still_count(repo):
    repo.write("src/a.py", "x = 1\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("src/a.py", "x = 2\n")
    repo.commit()

    assert changes(repo, base).added == {"src/a.py": {1: "x = 2"}}


def test_snapshot_leaves_the_real_index_alone(repo):
    repo.write("src/a.py", "x = 1\n")
    repo.commit()
    repo.write("src/a.py", "x = 2\n")
    repo.write("src/new.py", "y = 1\n")
    diff.snapshot(repo.path)

    assert git(repo.path, "diff", "--cached", "--name-only") == ""
    assert "?? src/new.py" in git(repo.path, "status", "--porcelain")


def test_non_code_files_are_ignored_without_reading_them(repo):
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("data/big.csv", "x\n" * 2_000_000)
    repo.write("notes.md", "hello\n")
    repo.write("src/a.py", "a\n")

    assert changes(repo, base).added == {"src/a.py": {1: "a"}}


def test_tool_work_dirs_are_ignored(repo):
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("mutants/src/a.py", "junk\n")
    repo.write("node_modules/x/a.js", "junk\n")
    repo.write(".venv/lib/a.py", "junk\n")

    assert changes(repo, base).added == {}


def test_works_in_repo_without_commits(repo):
    base = diff.snapshot(repo.path)
    repo.write("a.py", "a\n")

    assert changes(repo, base).added == {"a.py": {1: "a"}}


def test_snapshot_is_a_stable_fingerprint(repo):
    repo.write("a.py", "a\n")
    first = diff.snapshot(repo.path)
    assert diff.snapshot(repo.path) == first
    repo.write("a.py", "b\n")
    assert diff.snapshot(repo.path) != first


# --- hunk parsing ---------------------------------------------------------------------------


def test_pure_deletion_marks_the_lines_around_it(repo):
    repo.write("a.py", "a\nb\nc\nd\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("a.py", "a\nd\n")

    assert set(changes(repo, base).added["a.py"]) == {1, 2}


def test_handles_spaces_hangul_and_glob_characters_in_names(repo):
    for name in ["src/sp ace.py", "src/한글.py", "src/app/[id]/page.ts", "src/app/i/page.ts"]:
        repo.write(name, "a\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    for name in ["src/sp ace.py", "src/한글.py", "src/app/[id]/page.ts"]:
        repo.write(name, "b\n")

    assert changes(repo, base).added == {
        "src/app/[id]/page.ts": {1: "b"}, "src/sp ace.py": {1: "b"}, "src/한글.py": {1: "b"},
    }


def test_added_line_that_starts_with_plus_signs_stays_in_its_file(repo):
    repo.write("a.py", "x\n")
    repo.write("b.py", "y\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("a.py", "x\n++ y\nz\n")

    assert changes(repo, base).added == {"a.py": {2: "++ y", 3: "z"}}


def test_deleted_test_files_are_reported(repo):
    repo.write("tests/test_a.py", "def test_a(): pass\n")
    repo.write("src/a.test.ts", "it()\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    (repo.path / "tests" / "test_a.py").unlink()
    (repo.path / "src" / "a.test.ts").unlink()

    assert changes(repo, base).deleted_tests == ["src/a.test.ts", "tests/test_a.py"]


def test_stash_ref_changes_when_work_is_stashed(repo):
    repo.write("a.py", "a\n")
    repo.commit()
    before = diff.stash_ref(repo.path)
    repo.write("a.py", "b\n")
    git(repo.path, "stash", "-q")

    assert diff.stash_ref(repo.path) != before


def test_repo_root_finds_toplevel_from_subdir(repo):
    repo.write("src/deep/a.py", "")
    assert diff.repo_root(repo.path / "src" / "deep") == str(repo.path.resolve())


def test_repo_root_returns_none_outside_git(tmp_path):
    assert diff.repo_root(tmp_path) is None


# --- classification -------------------------------------------------------------------------


def test_classify_source_files():
    assert diff.classify("src/a.ts") == "js"
    assert diff.classify("src/a.tsx") == "js"
    assert diff.classify("lib/a.mjs") == "js"
    assert diff.classify("pkg/a.py") == "py"
    assert diff.classify("src/build/a.py") == "py"


def test_classify_skips_tests_configs_and_declarations():
    for path in [
        "src/a.test.ts", "src/a.spec.js", "src/__tests__/a.ts", "src/a.d.ts", "vitest.config.ts",
        "tests/test_a.py", "pkg/test_a.py", "pkg/a_test.py", "conftest.py", "node_modules/x/a.js",
        "README.md",
    ]:
        assert diff.classify(path) is None, path


def test_is_test():
    assert diff.is_test("src/a.test.ts") and diff.is_test("tests/test_a.py") and diff.is_test("pkg/a_test.py")
    assert not diff.is_test("src/a.ts") and not diff.is_test("conftest.py")


# --- suppressions and test integrity --------------------------------------------------------


def test_find_suppressions_flags_stryker_and_mutmut_markers():
    added = {
        "src/a.ts": {3: "  // Stryker disable next-line all", 4: "const x = 1;"},
        "pkg/a.py": {9: "    y = 2  # pragma: no mutate"},
        "pyproject.toml": {12: 'do_not_mutate = ["pkg/a.py"]'},
    }

    assert diff.find_suppressions(added) == [
        ("pkg/a.py", 9, "    y = 2  # pragma: no mutate"),
        ("pyproject.toml", 12, 'do_not_mutate = ["pkg/a.py"]'),
        ("src/a.ts", 3, "  // Stryker disable next-line all"),
    ]


def test_find_suppressions_ignores_markers_in_docs_and_tests():
    added = {
        "README.md": {1: "Never add `// Stryker disable` comments"},
        "docs/notes.md": {1: "do_not_mutate is a mutmut option"},
        "src/a.test.ts": {2: "// Stryker disable next-line all"},
    }
    assert diff.find_suppressions(added) == []


def test_find_test_skips_flags_skip_and_focus_markers_in_tests():
    added = {
        "src/a.test.ts": {1: "it.skip('x', () => {})", 2: "describe.only('y', () => {})", 3: "it('z')"},
        "tests/test_a.py": {4: "@pytest.mark.skip(reason='later')", 5: "    pytest.xfail('no')"},
        "src/a.ts": {6: "const skip = it.skip"},
    }
    assert [(p, n) for p, n, _ in diff.find_test_skips(added)] == [
        ("src/a.test.ts", 1), ("src/a.test.ts", 2), ("tests/test_a.py", 4), ("tests/test_a.py", 5),
    ]


def test_mutmut_config_change_is_detected_even_without_key_names(repo):
    repo.write("pyproject.toml", '[tool.mutmut]\ndo_not_mutate = [\n    "a.py",\n]\n')
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("pyproject.toml", '[tool.mutmut]\ndo_not_mutate = [\n    "a.py",\n    "b.py",\n]\n')

    assert diff.mutation_config_changes(repo.path, base) == ["pyproject.toml"]


def test_unrelated_pyproject_change_is_not_a_mutation_config_change(repo):
    repo.write("pyproject.toml", '[project]\nname = "x"\n\n[tool.mutmut]\nsource_paths = ["src"]\n')
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("pyproject.toml", '[project]\nname = "y"\n\n[tool.mutmut]\nsource_paths = ["src"]\n')

    assert diff.mutation_config_changes(repo.path, base) == []


def test_classify_vue_svelte_and_ordinary_dirs_named_build_or_test():
    assert diff.classify("src/App.vue") == "js"
    assert diff.classify("src/Card.svelte") == "js"
    assert diff.classify("src/commands/build/run.ts") == "js"
    assert diff.classify("src/commands/test/run.ts") == "js"


def test_classify_skips_js_support_code():
    for path in [
        "src/Button.stories.tsx", "src/__mocks__/api.ts", "e2e/login.ts", "cypress/support/e2e.ts",
        "src/setupTests.ts", "src/render.test-utils.tsx", ".eslintrc.cjs", "src/types.d.mts", "test/helpers.ts",
    ]:
        assert diff.classify(path) is None, path


def test_same_size_edit_right_after_commit_is_seen(repo):
    for _ in range(20):
        repo.write("src/a.py", "x < 5\n")
        repo.commit()
        base = diff.snapshot(repo.path)
        repo.write("src/a.py", "x > 5\n")
        assert changes(repo, base).added == {"src/a.py": {1: "x > 5"}}


def test_marker_words_inside_strings_are_not_suppressions():
    added = {
        "mutation_gate/gate.py": {125: '    "- 억제 주석(Stryker disable, pragma: no mutate)은 금지."'},
        "src/msg.ts": {3: 'const help = "never add Stryker disable comments";'},
    }
    assert diff.find_suppressions(added) == []


@pytest.mark.parametrize("path, lang", [
    ("src/lib.rs", "rust"), ("crates/core/src/parse.rs", "rust"),
    ("pkg/price/price.go", "go"), ("main.go", "go"),
    ("src/main/java/com/x/Price.java", "jvm"), ("src/main/kotlin/x/Price.kt", "jvm"),
    ("src/Shop/Price.cs", "dotnet"),
    ("src/main/scala/x/Price.scala", "scala"),
])
def test_classify_other_languages(path, lang):
    assert diff.classify(path) == lang


@pytest.mark.parametrize("path", [
    "tests/integration.rs", "benches/b.rs", "examples/demo.rs",
    "pkg/price/price_test.go",
    "src/test/java/com/x/PriceTest.java", "src/test/kotlin/x/PriceTest.kt",
    "tests/Shop.Tests/PriceTests.cs", "src/Shop.Tests/PriceTest.cs",
    "src/test/scala/x/PriceSpec.scala",
])
def test_other_languages_tests_are_not_source(path):
    assert diff.classify(path) is None
    assert diff.is_test(path)


@pytest.mark.parametrize("line", [
    "    #[ignore]", "\tt.Skip(\"later\")", "    @Disabled", "    @Ignore", "    [Fact(Skip = \"x\")]", '  ignore("x") {',
])
def test_find_test_skips_in_other_languages(line):
    added = {"src/test/x/ATest.java": {1: line}, "a_test.go": {1: line}, "tests/a.rs": {1: line},
             "tests/A.Tests/ATests.cs": {1: line}, "src/test/scala/ASpec.scala": {1: line}}
    assert diff.find_test_skips(added)
