import shutil
from pathlib import Path

import pytest

from conftest import git
from mutation_gate import diff, tools
from mutation_gate.adapters import pit

FIXTURE = Path(__file__).parent / "fixtures" / "jvm-mini"
# Maven's local repository for these tests: next to the fixture (gitignored), so ~/.m2 stays clean.
M2 = FIXTURE / ".m2"
HAVE_JVM = bool(pit._java_home() and tools.find("mvn", pit.MAVEN_DIRS))
needs_jvm = pytest.mark.skipif(not HAVE_JVM, reason="needs a JDK and Maven (see tests/setup_fixtures.sh)")

PRICE = "src/main/java/shop/Price.java"
PRICE_TEST = "src/test/java/shop/PriceTest.java"
TEST_HEADER = """package shop;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class PriceTest {
"""
WEAK_TEST = TEST_HEADER + "    @Test\n    void adult() {\n        assertTrue(Price.isAdult(30));\n    }\n}\n"
STRONG_TEST = TEST_HEADER + """    @Test
    void boundary() {
        assertFalse(Price.isAdult(17));
        assertTrue(Price.isAdult(18));
    }
}
"""
FAILING_TEST = TEST_HEADER + "    @Test\n    void adult() {\n        assertTrue(Price.isAdult(3));\n    }\n}\n"


@pytest.fixture
def maven(repo, gate_home, monkeypatch):
    """An empty repo whose Maven runs use the tests' own local repository."""
    monkeypatch.setenv("MAVEN_ARGS", f"-Dmaven.repo.local={M2}")
    repo.write(".gitignore", (FIXTURE / ".gitignore").read_text())
    return repo


@pytest.fixture
def jvm(maven):
    shutil.copy(FIXTURE / "pom.xml", maven.path / "pom.xml")
    shutil.copytree(FIXTURE / "src", maven.path / "src")
    return maven


def _fake_jdk(tmp_path, monkeypatch):
    home = tmp_path / "fake-jdk"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("JAVA_HOME", str(home))


def _survivors(result):
    return [m for m in result.mutants if m.status == "undetected"]


@needs_jvm
def test_weak_test_leaves_survivors_with_details(jvm):
    jvm.write(PRICE_TEST, WEAK_TEST)

    result = pit.run(str(jvm.path), {PRICE: {5}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert result.mutants and {m.line for m in result.mutants} == {5}
    boundary = [m for m in result.mutants if m.mutator == "ConditionalsBoundaryMutator"]
    assert boundary and boundary[0].status == "undetected"
    assert boundary[0].original == "return age >= 18;"
    assert boundary[0].replacement == "changed conditional boundary"
    assert boundary[0].context == "isAdult" and boundary[0].path == PRICE
    assert any(m.mutator == "NegateConditionalsMutator" and m.status == "detected" for m in result.mutants)


@needs_jvm
def test_strong_boundary_test_detects_everything(jvm):
    jvm.write(PRICE_TEST, STRONG_TEST)

    result = pit.run(str(jvm.path), {PRICE: set(range(1, 8))}, budget=180)

    assert result.error is None and result.failure is None, result
    assert result.mutants and not _survivors(result)


@needs_jvm
def test_only_changed_lines_count(jvm):
    jvm.write(PRICE, """package shop;

public final class Price {
    static boolean isAdult(int age) {
        return age >= 18;
    }

    static int discount(int total) {
        return total > 100 ? 10 : 0;
    }
}
""")
    jvm.write(PRICE_TEST, TEST_HEADER + """    @Test
    void runs() {
        Price.isAdult(30);
        Price.discount(500);
    }
}
""")

    result = pit.run(str(jvm.path), {PRICE: {9}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert result.mutants and {m.line for m in result.mutants} == {9}
    assert all(m.context == "discount" for m in result.mutants)


@needs_jvm
def test_failing_tests_are_a_failure(jvm):
    jvm.write(PRICE_TEST, FAILING_TEST)

    result = pit.run(str(jvm.path), {PRICE: {5}}, budget=180)

    assert result.error is None and not result.mutants
    assert "the tests are failing" in result.failure
    assert "shop.PriceTest adult()" in result.failure


@needs_jvm
def test_module_without_tests_is_a_failure(jvm):
    result = pit.run(str(jvm.path), {PRICE: {5}}, budget=180)

    assert result.error is None
    assert "no test runs" in result.failure


@needs_jvm
def test_code_that_does_not_compile_is_a_tool_error(jvm):
    jvm.write(PRICE_TEST, WEAK_TEST)
    jvm.write(PRICE, (FIXTURE / PRICE).read_text().replace("age >= 18", "age >="))

    result = pit.run(str(jvm.path), {PRICE: {5}}, budget=180)

    assert result.failure is None
    assert result.error_kind == "crash" and "does not compile" in result.error
    assert "Price.java" in result.error


@needs_jvm
def test_interface_has_nothing_to_mutate(jvm):
    jvm.write(PRICE_TEST, WEAK_TEST)
    jvm.write("src/main/java/shop/Shape.java", "package shop;\n\npublic interface Shape {\n    double area();\n}\n")

    result = pit.run(str(jvm.path), {"src/main/java/shop/Shape.java": {4}}, budget=180)

    assert result == pit.AdapterResult()


@needs_jvm
def test_run_leaves_only_target_and_deletes_the_report(jvm, gate_home):
    jvm.write(PRICE_TEST, WEAK_TEST)
    git(jvm.path, "add", "-A")
    jvm.commit()

    pit.run(str(jvm.path), {PRICE: {5}}, budget=180)

    assert git(jvm.path, "status", "--porcelain") == ""
    ignored = git(jvm.path, "status", "--porcelain", "--ignored")
    assert ignored == "!! target/"
    assert not list(gate_home.rglob("mutations.xml"))


@needs_jvm
def test_junit4_project_runs_without_the_junit5_plugin(jvm):
    _junit4(jvm)
    assert not pit._uses_platform(jvm.path, [jvm.path])

    result = pit.run(str(jvm.path), {PRICE: {5}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert result.mutants and not _survivors(result)


MULTI_ROOT = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>multi</artifactId>
  <version>1.0</version>
  <packaging>pom</packaging>
  <modules>
    <module>core</module>
    <module>app</module>
  </modules>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.14.4</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
"""
MODULE_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>example</groupId><artifactId>multi</artifactId><version>1.0</version></parent>
  <artifactId>{name}</artifactId>
  {deps}
</project>
"""


@needs_jvm
def test_module_of_a_multi_module_build_gets_its_sibling_modules_built(maven):
    jvm = maven
    jvm.write("pom.xml", MULTI_ROOT)
    jvm.write("core/pom.xml", MODULE_POM.format(name="core", deps=""))
    jvm.write("core/src/main/java/lib/Core.java", "package lib;\n\npublic class Core {\n    public static int twice(int x) {\n        return x * 2;\n    }\n}\n")
    jvm.write("app/pom.xml", MODULE_POM.format(
        name="app", deps="<dependencies><dependency><groupId>example</groupId><artifactId>core</artifactId><version>1.0</version></dependency></dependencies>"))
    app = "app/src/main/java/app/App.java"
    jvm.write(app, "package app;\n\nimport lib.Core;\n\npublic class App {\n    public static boolean big(int x) {\n        return Core.twice(x) > 10;\n    }\n}\n")
    jvm.write("app/src/test/java/app/AppTest.java", """package app;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class AppTest {
    @Test
    void big() {
        assertTrue(App.big(100));
    }
}
""")

    result = pit.run(str(jvm.path), {app: {7}}, budget=240)

    assert result.error is None and result.failure is None, result
    assert result.mutants and all(m.path == app and m.line == 7 for m in result.mutants)
    assert any(m.mutator == "ConditionalsBoundaryMutator" and m.status == "undetected" for m in result.mutants)


KOTLIN_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>kt-mini</artifactId>
  <version>1.0</version>
  <properties>
    <kotlin.version>2.4.20</kotlin.version>
    <kotlin.compiler.jvmTarget>17</kotlin.compiler.jvmTarget>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.jetbrains.kotlin</groupId>
      <artifactId>kotlin-stdlib</artifactId>
      <version>${kotlin.version}</version>
    </dependency>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.14.4</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <sourceDirectory>src/main/kotlin</sourceDirectory>
    <testSourceDirectory>src/test/kotlin</testSourceDirectory>
    <plugins>
      <plugin>
        <groupId>org.jetbrains.kotlin</groupId>
        <artifactId>kotlin-maven-plugin</artifactId>
        <version>${kotlin.version}</version>
        <executions>
          <execution><id>compile</id><goals><goal>compile</goal></goals></execution>
          <execution><id>test-compile</id><goals><goal>test-compile</goal></goals></execution>
        </executions>
      </plugin>
    </plugins>
  </build>
</project>
"""
KOTLIN_SRC = """package shop

fun isAdult(age: Int): Boolean {
    return age >= 18
}

class Cart(private val items: List<Int>) {
    fun total(): Int = items.sum()
}
"""
KOTLIN_TEST = """package shop

import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class PriceTest {
    @Test
    fun adult() {
        assertTrue(isAdult(30))
        Cart(listOf(1, 2)).total()
    }
}
"""


@needs_jvm
def test_kotlin_file_is_mutated_and_mapped_back(maven):
    jvm = maven
    kt = "src/main/kotlin/shop/Price.kt"
    jvm.write("pom.xml", KOTLIN_POM)
    jvm.write(kt, KOTLIN_SRC)
    jvm.write("src/test/kotlin/shop/PriceTest.kt", KOTLIN_TEST)

    result = pit.run(str(jvm.path), {kt: set(range(1, 11))}, budget=240)

    assert result.error is None and result.failure is None, result
    assert {m.path for m in result.mutants} == {kt}
    # Top-level functions compile into PriceKt, the class into Cart: both map back to Price.kt.
    assert {(m.line, m.context) for m in result.mutants} >= {(4, "isAdult"), (8, "total")}
    total = [m for m in result.mutants if m.line == 8]
    assert total[0].original == "fun total(): Int = items.sum()" and total[0].status == "undetected"


def test_kotlin_and_java_sources_are_located():
    assert diff.classify("src/main/kotlin/shop/Price.kt") == "jvm"
    assert diff.classify("src/test/kotlin/shop/PriceTest.kt") is None
    package, globs = pit._targets(KOTLIN_SRC, "src/main/kotlin/shop/Price.kt")
    assert package == "shop"
    assert {"shop.PriceKt", "shop.PriceKt$*", "shop.Cart", "shop.Price"} <= set(globs)
    package, globs = pit._targets("class Top {}\n", "Top.java")
    assert package == "" and "Top" in globs


def test_missing_maven_is_a_tool_error_with_install_hint(repo, gate_home, tmp_path, monkeypatch):
    shutil.copy(FIXTURE / "pom.xml", repo.path / "pom.xml")
    repo.write(PRICE, (FIXTURE / PRICE).read_text())
    _fake_jdk(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    if shutil.which("mvn"):
        pytest.skip("mvn on /usr/bin")

    result = pit.run(str(repo.path), {PRICE: {5}}, budget=30)

    assert result.failure is None and result.error_kind == "missing"
    assert "Maven not found" in result.error and "~/.local/opt" in result.error
    assert result.cacheable is False


def test_tracked_maven_binary_is_refused(repo, gate_home, tmp_path, monkeypatch):
    shutil.copy(FIXTURE / "pom.xml", repo.path / "pom.xml")
    repo.write(PRICE, (FIXTURE / PRICE).read_text())
    repo.write("bin/mvn", "#!/bin/sh\ntouch PWNED\n")
    (repo.path / "bin/mvn").chmod(0o755)
    git(repo.path, "add", "-f", "bin/mvn")
    _fake_jdk(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"{repo.path / 'bin'}:/usr/bin:/bin")

    result = pit.run(str(repo.path), {PRICE: {5}}, budget=30)

    assert result.error_kind == "missing" and "git tracks it" in result.error
    assert not (repo.path / "PWNED").exists()


def test_gradle_project_is_reported_as_unsupported(repo, gate_home):
    repo.write("build.gradle.kts", 'plugins { java }\n')
    repo.write(PRICE, (FIXTURE / PRICE).read_text())

    result = pit.run(str(repo.path), {PRICE: {5}}, budget=30)

    assert result.error_kind == "missing" and not result.mutants
    assert "Gradle projects need the info.solidsoft.pitest plugin" in result.error and PRICE in result.error


@needs_jvm
def test_whole_run_timeout_is_reported(jvm):
    jvm.write(PRICE_TEST, WEAK_TEST)

    result = pit.run(str(jvm.path), {PRICE: {5}}, budget=1)

    assert result.error_kind == "timeout" and not result.mutants


JUNIT4_TEST = """package shop;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PriceTest {
    @Test
    public void boundary() {
        assertFalse(Price.isAdult(17));
        assertTrue(Price.isAdult(18));
    }
}
"""


def _junit4(jvm):
    pom = (FIXTURE / "pom.xml").read_text()
    pom = pom.replace("<groupId>org.junit.jupiter</groupId>", "<groupId>junit</groupId>")
    pom = pom.replace("<artifactId>junit-jupiter</artifactId>", "<artifactId>junit</artifactId>")
    jvm.write("pom.xml", pom.replace("<version>5.14.4</version>", "<version>4.13.2</version>"))
    jvm.write(PRICE_TEST, JUNIT4_TEST)


@needs_jvm
@pytest.mark.parametrize("framework", ["junit5", "junit4"])
def test_wrong_guess_about_the_test_framework_is_retried(jvm, monkeypatch, framework):
    # The guess comes from imports and poms; PIT's own error corrects it.
    if framework == "junit4":
        _junit4(jvm)
        monkeypatch.setattr(pit, "_uses_platform", lambda root, modules: True)
    else:
        jvm.write(PRICE_TEST, STRONG_TEST)
        monkeypatch.setattr(pit, "_uses_platform", lambda root, modules: False)

    result = pit.run(str(jvm.path), {PRICE: {5}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert result.mutants and not _survivors(result)


POM_WITH_PIT_CONFIG = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <build><plugins><plugin>
    <groupId>org.pitest</groupId><artifactId>pitest-maven</artifactId>
    <configuration><excludedMethods><param>isAdult</param></excludedMethods></configuration>
  </plugin></plugins></build>
</project>
"""


def test_pom_with_its_own_pitest_configuration_is_noted(tmp_path):
    (tmp_path / "pom.xml").write_text(POM_WITH_PIT_CONFIG)
    notes = pit._pom_pitest_notes(tmp_path)
    assert notes and "pom.xml" in notes[0] and "pitest-maven" in notes[0]


def test_pom_without_pitest_configuration_has_no_note(tmp_path):
    (tmp_path / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion></project>')
    assert pit._pom_pitest_notes(tmp_path) == []
