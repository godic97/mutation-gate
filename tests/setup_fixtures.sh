#!/bin/sh
# Install the tools the integration tests drive. Language toolchains are optional: their tests skip without them.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
(cd "$here/fixtures/js-mini" && npm install --silent --no-audit --no-fund)
(cd "$here/fixtures/js-jest-mini" && npm install --silent --no-audit --no-fund)
uv venv -q "$here/fixtures/py-mini/.venv"
uv pip install -q --python "$here/fixtures/py-mini/.venv/bin/python" -r "$here/fixtures/py-mini/requirements.txt"
# Go + gremlins (Go itself from https://go.dev/dl/, extracted to ~/.local/go)
go_bin="$(command -v go || echo "$HOME/.local/go/bin/go")"
if [ -x "$go_bin" ]; then "$go_bin" install github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0; else echo "skipping Go: install Go into ~/.local/go first" >&2; fi
# .NET SDK (dotnet-install.sh --channel 8.0 --install-dir ~/.dotnet) + Stryker.NET 4.16.0 (5.x needs the .NET 10 SDK)
dotnet_bin="$(command -v dotnet || echo "$HOME/.dotnet/dotnet")"
if [ -x "$dotnet_bin" ]; then
  command -v dotnet-stryker >/dev/null || [ -x "$HOME/.dotnet/tools/dotnet-stryker" ] ||
    DOTNET_ROOT="$(dirname "$(readlink -f "$dotnet_bin")")" DOTNET_CLI_TELEMETRY_OPTOUT=1 "$dotnet_bin" tool install -g dotnet-stryker --version 4.16.0
else echo "skipping .NET: install the SDK into ~/.dotnet first (dotnet-install.sh --channel 8.0 --install-dir ~/.dotnet)" >&2; fi
# Rust + cargo-mutants (rustup --no-modify-path --profile minimal installs into ~/.cargo)
cargo_bin="$(command -v cargo || echo "$HOME/.cargo/bin/cargo")"
if [ -x "$cargo_bin" ]; then
  command -v cargo-mutants >/dev/null || [ -x "$HOME/.cargo/bin/cargo-mutants" ] || "$cargo_bin" install --locked cargo-mutants --version 27.1.0
else echo "skipping Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal" >&2; fi
# Java/Kotlin (PIT): a JDK (JAVA_HOME or ~/.local/opt/jdk*) and Maven; Maven fills tests/fixtures/jvm-mini/.m2 on the first test run
mvn_ver=3.9.16
command -v mvn >/dev/null || [ -x "$HOME/.local/opt/apache-maven-$mvn_ver/bin/mvn" ] || {
  mkdir -p "$HOME/.local/opt"
  curl -fsSL "https://archive.apache.org/dist/maven/maven-3/$mvn_ver/binaries/apache-maven-$mvn_ver-bin.tar.gz" | tar -xz -C "$HOME/.local/opt"
}
# Scala (Stryker4s): sbt runner in user scope; shares the JDK with the Java tests
[ -x "$HOME/.local/opt/sbt/bin/sbt" ] || { mkdir -p "$HOME/.local/opt" && curl -fsSL https://github.com/sbt/sbt/releases/download/v2.0.10/sbt-2.0.10.tgz | tar -xz -C "$HOME/.local/opt"; }
