"""Find toolchains installed on PATH or in user-scope locations.

Hooks run with whatever PATH Claude Code was started with, and toolchains installed without
touching shell profiles (rustup --no-modify-path, Go and JDK tarballs, dotnet-install.sh) are
not on it. Adapters look in those places too.
"""

import glob
import os
import shutil


def _expand(pattern):
    return sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)  # newest version first


def find(name, extra_dirs=()):
    """Path of executable `name`: PATH first, then each extra directory (globs and ~ allowed)."""
    found = shutil.which(name)
    if found:
        return found
    for pattern in extra_dirs:
        for directory in _expand(pattern):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def env_with(path_dirs=(), **extra):
    """os.environ with `path_dirs` in front of PATH, colour off, and `extra` variables set."""
    path = os.pathsep.join([*path_dirs, os.environ.get("PATH", "")])
    return {**os.environ, "PATH": path, "NO_COLOR": "1", "FORCE_COLOR": "0", **extra}


def java_home():
    """JAVA_HOME if set, else a JDK unpacked under ~/.local/opt (macOS bundle or plain layout)."""
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    for pattern in ("~/.local/opt/jdk*/Contents/Home", "~/.local/opt/jdk*"):
        for home in _expand(pattern):
            if os.path.isfile(os.path.join(home, "bin", "java")):
                return home
    return None


# Where user-scope toolchains land; added to PATH for the project's own test commands.
TOOLCHAIN_DIRS = [
    "~/.cargo/bin", "~/.local/go/bin", "~/go/bin", "~/.dotnet", "~/.dotnet/tools",
    "~/.local/opt/apache-maven-*/bin", "~/.local/opt/sbt/bin",
]


def toolchain_env():
    """Environment for running a project's tests with every user-scope toolchain reachable."""
    dirs = [d for pattern in TOOLCHAIN_DIRS for d in _expand(pattern)[:1]]
    extra = {}
    home = java_home()
    if home:
        dirs.insert(0, os.path.join(home, "bin"))
        extra["JAVA_HOME"] = home
    dotnet = os.path.expanduser("~/.dotnet")
    if os.path.isdir(dotnet) and not os.environ.get("DOTNET_ROOT"):
        extra["DOTNET_ROOT"] = dotnet
    return env_with(dirs, **extra)
