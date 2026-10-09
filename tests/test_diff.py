from mutation_gate import diff


def test_added_lines_reports_new_and_modified_lines_since_base(repo):
    repo.write("src/a.ts", "one\ntwo\nthree\n")
    base = repo.commit()
    repo.write("src/a.ts", "one\nTWO\nthree\nfour\n")

    added = diff.added_lines(repo.path, base)

    assert added == {"src/a.ts": {2: "TWO", 4: "four"}}


def test_added_lines_includes_changes_committed_after_base(repo):
    repo.write("src/a.py", "x = 1\n")
    base = repo.commit()
    repo.write("src/a.py", "x = 2\n")
    repo.commit()

    assert diff.added_lines(repo.path, base) == {"src/a.py": {1: "x = 2"}}


def test_added_lines_includes_untracked_files_whole(repo):
    base = repo.commit()
    repo.write("src/new.py", "a\nb\n")

    assert diff.added_lines(repo.path, base) == {"src/new.py": {1: "a", 2: "b"}}


def test_added_lines_skips_pure_deletions(repo):
    repo.write("a.py", "a\nb\nc\n")
    base = repo.commit()
    repo.write("a.py", "a\nc\n")

    assert diff.added_lines(repo.path, base) == {}


def test_added_lines_ignores_tool_work_dirs(repo):
    base = repo.commit()
    repo.write("mutants/src/a.py", "junk\n")
    repo.write(".stryker-tmp/x.js", "junk\n")

    assert diff.added_lines(repo.path, base) == {}


def test_added_lines_works_in_repo_without_commits(repo):
    base = diff.head_sha(repo.path)
    repo.write("a.py", "a\n")

    assert diff.added_lines(repo.path, base) == {"a.py": {1: "a"}}


def test_repo_root_finds_toplevel_from_subdir(repo):
    repo.write("src/deep/a.py", "")
    assert diff.repo_root(repo.path / "src" / "deep") == str(repo.path.resolve())


def test_repo_root_returns_none_outside_git(tmp_path):
    assert diff.repo_root(tmp_path) is None


def test_fingerprint_changes_when_untracked_content_changes(repo):
    base = repo.commit()
    repo.write("a.py", "a\n")
    first = diff.fingerprint(repo.path, base)
    repo.write("a.py", "b\n")

    assert diff.fingerprint(repo.path, base) != first


def test_fingerprint_stable_when_nothing_changes(repo):
    repo.write("a.py", "a\n")
    base = repo.commit()
    repo.write("a.py", "b\n")

    assert diff.fingerprint(repo.path, base) == diff.fingerprint(repo.path, base)


def test_classify_source_files():
    assert diff.classify("src/a.ts") == "js"
    assert diff.classify("src/a.tsx") == "js"
    assert diff.classify("lib/a.mjs") == "js"
    assert diff.classify("pkg/a.py") == "py"


def test_classify_skips_tests_configs_and_declarations():
    for path in [
        "src/a.test.ts",
        "src/a.spec.js",
        "src/__tests__/a.ts",
        "src/a.d.ts",
        "vitest.config.ts",
        "tests/test_a.py",
        "pkg/test_a.py",
        "pkg/a_test.py",
        "conftest.py",
        "node_modules/x/a.js",
        "README.md",
    ]:
        assert diff.classify(path) is None, path


def test_find_suppressions_flags_stryker_and_mutmut_markers():
    added = {
        "src/a.ts": {3: "  // Stryker disable next-line all", 4: "const x = 1;"},
        "pkg/a.py": {9: "    y = 2  # pragma: no mutate"},
        "pyproject.toml": {12: 'do_not_mutate = ["pkg/a.py"]'},
    }

    found = diff.find_suppressions(added)

    assert found == [
        ("pkg/a.py", 9, "    y = 2  # pragma: no mutate"),
        ("pyproject.toml", 12, 'do_not_mutate = ["pkg/a.py"]'),
        ("src/a.ts", 3, "  // Stryker disable next-line all"),
    ]


def test_find_suppressions_ignores_mutmut_keys_outside_config_files():
    added = {"docs/notes.md": {1: "do_not_mutate is a mutmut option"}}
    assert diff.find_suppressions(added) == []


def test_find_suppressions_ignores_markers_in_docs_and_tests():
    added = {
        "README.md": {1: "Never add `// Stryker disable` comments"},
        "src/a.test.ts": {2: "// Stryker disable next-line all"},
        "tests/test_a.py": {3: "x = 1  # pragma: no mutate"},
    }
    assert diff.find_suppressions(added) == []


def test_added_lines_skips_large_untracked_files(repo):
    base = repo.commit()
    repo.write("data/big.csv", "x\n" * 2_000_000)
    repo.write("src/a.py", "a\n")
    assert diff.added_lines(repo.path, base) == {"src/a.py": {1: "a"}}


def test_added_lines_handles_spaces_and_hangul_in_names(repo):
    repo.write("src/sp ace.py", "a\n")
    repo.write("src/한글.py", "a\n")
    base = repo.commit()
    repo.write("src/sp ace.py", "b\n")
    repo.write("src/한글.py", "b\n")

    assert diff.added_lines(repo.path, base) == {"src/sp ace.py": {1: "b"}, "src/한글.py": {1: "b"}}


def test_added_line_that_starts_with_plus_signs_stays_in_its_file(repo):
    repo.write("a.py", "x\n")
    repo.write("b.py", "y\n")
    base = repo.commit()
    repo.write("a.py", "x\n++ y\nz\n")

    assert diff.added_lines(repo.path, base) == {"a.py": {2: "++ y", 3: "z"}}


def test_binary_change_does_not_capture_following_lines(repo):
    repo.write("a.bin", "")
    (repo.path / "a.bin").write_bytes(b"\x00\x01")
    repo.write("b.py", "x\n")
    base = repo.commit()
    (repo.path / "a.bin").write_bytes(b"\x00\x02")
    repo.write("b.py", "y\n")

    assert diff.added_lines(repo.path, base) == {"b.py": {1: "y"}}


def test_added_lines_with_glob_characters_in_path(repo):
    repo.write("src/app/[id]/page.ts", "a\n")
    repo.write("src/app/i/page.ts", "a\n")
    base = repo.commit()
    repo.write("src/app/[id]/page.ts", "b\n")

    assert diff.added_lines(repo.path, base) == {"src/app/[id]/page.ts": {1: "b"}}
