"""Run both native completion functions against an isolated bgx registry."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
BINARY = Path(os.environ.get("BGX_BIN", ROOT / "build/bgx")).resolve()

BASH = r'''
source "$1"
shift
complete -p bgx >/dev/null || exit 1
COMP_WORDS=("$@")
COMP_CWORD=$((${#COMP_WORDS[@]} - 1))
_bgx_complete
for candidate in "${COMPREPLY[@]}"; do printf '%s\n' "$candidate"; done
'''
ZSH = r'''
fpath=("$1" $fpath)
shift
autoload -Uz _bgx
words=("$@")
CURRENT=${#words}
# compadd normally submits matches to ZLE. Capture them with its literal-prefix
# behavior here so the real autoloaded function can run without an interactive UI.
compadd() {
  [[ $1 == -- ]] || return 1
  shift
  local candidate
  for candidate in "$@"; do
    [[ $candidate == "${words[CURRENT]}"* ]] && print -r -- "$candidate"
  done
  return 0
}
_bgx
'''


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bgx-completion-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.env = dict(os.environ, BGX_STATE_HOME=str(self.state))
        self.shells = [shell for shell in ["bash", "zsh"] if shutil.which(shell)]
        if not self.shells:
            self.skipTest("requires bash or zsh")

    def cli(self, *args):
        result = subprocess.run([str(BINARY), *args], env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def complete(self, shell, *args, env=None):
        script = BASH if shell == "bash" else ZSH
        path = ROOT / "completions/bgx.bash" if shell == "bash" else ROOT / "completions"
        flags = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
        result = subprocess.run([shutil.which(shell), *flags, "-c", script, "completion-test",
                                 str(path), str(BINARY), *args], env=env or self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, (shell, args, result.stderr))
        self.assertEqual(result.stderr, "", (shell, args))
        return set(result.stdout.splitlines())

    def make_runs(self):
        for name in ["web", "Web", "worker", "stop"]:
            self.cli("start", name, "--", "/bin/echo", "done")
            self.cli("stop", name)

    def test_commands_options_and_separator(self):
        commands = {"start", "s", "stop", "rename", "list", "ls", "info", "log",
                    "remove", "rm", "prune", "help", "version", "-h", "--help", "-V", "--version"}
        cases = [(("",), commands), (("help", ""), commands),
                 (("--help", "re"), {"rename", "remove"}),
                 (("help", "stop", ""), set()),
                 (("s", ""), {"--", "-h", "--help"}),
                 (("start", "web", ""), {"--", "-h", "--help"}),
                 (("start", "--", ""), set()),
                 (("s", "web", "--", "echo", "--"), set()),
                 (("log", "--tail", ""), set()),
                 (("log", "--tail", "5", "-"), {"-h", "--help", "-f", "--follow"}),
                 (("log", "-f", "-"), {"-h", "--help", "--tail"}),
                 (("rm", "-"), {"-h", "--help", "--force"}),
                 (("rm", "--force", "-"), {"-h", "--help"}),
                 (("prune", "--dry-run", "-"), {"-h", "--help"}),
                 (("list", "-"), {"-h", "--help"}),
                 (("ps", ""), set()), (("logs", ""), set())]
        for shell in self.shells:
            for args, expected in cases:
                with self.subTest(shell=shell, args=args):
                    self.assertEqual(self.complete(shell, *args), expected)
        self.assertFalse(self.state.exists())

    def test_names_multiple_operands_and_rename_destination(self):
        self.make_runs()
        cases = [(("stop", "w"), {"web", "worker"}),
                 (("info", "W"), {"Web"}),
                 (("rename", "w"), {"web", "worker"}),
                 (("rename", "web", "w"), set()),
                 (("info", "web", "w"), set()),
                 (("log", "web", "w"), set()),
                 (("start", "w"), set()),
                 (("stop", "web", "w"), {"worker"}),
                 (("remove", "--force", "web", "w"), {"worker"}),
                 (("rm", "Web", "web", "w"), {"worker"}),
                 (("log", "--tail", "5", "w"), {"web", "worker"}),
                 (("stop", "s"), {"stop"})]
        before = {str(p): p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
        for shell in self.shells:
            for args, expected in cases:
                with self.subTest(shell=shell, args=args):
                    self.assertEqual(self.complete(shell, *args), expected)
        self.assertEqual({str(p): p.read_bytes() for p in self.state.rglob("*") if p.is_file()}, before)

    def test_names_refresh_after_rename_and_removal(self):
        self.make_runs()
        for shell in self.shells:
            self.assertEqual(self.complete(shell, "stop", "w"), {"web", "worker"})
        self.cli("rename", "web", "renamed")
        self.cli("rm", "worker")
        for shell in self.shells:
            self.assertEqual(self.complete(shell, "stop", "w"), set())
            self.assertEqual(self.complete(shell, "stop", "r"), {"renamed"})

    def test_partial_list_and_configured_state(self):
        self.make_runs()
        (self.state / "instances/web/meta.json").write_text("{broken")
        for shell in self.shells:
            self.assertEqual(self.complete(shell, "rm", "w"), {"web", "worker"})
            other = self.base / "other-state"
            self.assertEqual(self.complete(shell, "stop", "w",
                                           env=dict(self.env, BGX_STATE_HOME=str(other))), set())
            self.assertFalse(other.exists())

    @unittest.skipUnless(shutil.which("zsh"), "requires zsh")
    def test_zsh_compinit_discovers_registration(self):
        result = subprocess.run([shutil.which("zsh"), "-f", "-c", r'''
fpath=("$1" $fpath)
autoload -Uz compinit
compinit -u -D
[[ ${_comps[bgx]} == _bgx ]]
''', "completion-test", str(ROOT / "completions")], env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
