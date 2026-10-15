import tempfile, subprocess, os, json
from pathlib import Path

# Create a test workspace
ws = Path(tempfile.mkdtemp(prefix="ws-")).resolve()
probe_file = ws / "test.txt"
probe_file.write_text("hello\n", encoding="utf-8")

# Create native home
home = Path(tempfile.mkdtemp(prefix="home-")).resolve()
settings_file = home / ".gemini/antigravity-cli/settings.json"
settings_file.parent.mkdir(parents=True)

ws_win = str(ws)
ws_posix = ws_win.replace("\\", "/")
pf_win = str(probe_file)
pf_posix = pf_win.replace("\\", "/")

settings = {
    "permissions": {
        "allow": [
            f"read_file({pf_win})",
            f"read_file({pf_posix})",
            f"read_file({ws_win})",
            f"read_file({ws_posix})",
            f"write_file({ws_win})",
            f"write_file({ws_posix})",
            "command(python -m pytest)"
        ],
        "ask": [],
        "deny": []
    },
    "trustedWorkspaces": [ws_win, ws_posix],
    "toolPermission": "strict",
    "allowNonWorkspaceAccess": False,
    "useG1Credits": False
}
settings_file.write_text(json.dumps(settings), encoding="utf-8")

agent_name = "hermes-project-probe"
agent_dir = home / f".gemini/config/agents/{agent_name}"
agent_dir.mkdir(parents=True)
agent_md = agent_dir / "agent.md"
agent_md.write_text(f"""---
name: {agent_name}
description: Hermes bounded project developer
tools:
  - grep_search
  - replace_file_content
  - run_command
  - view_file
  - write_to_file
mainAgent: true
subagent: false
commandExecutionPolicy: sandbox
mcpServers: []
skills: []
plugins: []
---
Work only inside the active workspace. Read the relevant files before editing.
""")

from execution.proxy import get_cli_proxy_env
env = os.environ.copy()
env.update(get_cli_proxy_env())
env["USERPROFILE"] = str(home)
env["HOME"] = str(home)
env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"

cmd = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
       "--model", "gemini-3.8-flash-high", "--agent", agent_name, "--mode", "accept-edits"]

input_turn = json.dumps({"prompt": "Use view_file to read test.txt and reply with content"}) + "\n"

try:
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(ws), env=env, text=True, encoding="utf-8")
    stdout, stderr = p.communicate(input=input_turn, timeout=30)
    print("STDOUT len:", len(stdout))
    print("STDOUT:\n", stdout[:1000])
except Exception as e:
    print("Error:", e)

log_dir = home / ".gemini/antigravity-cli/log"
if log_dir.exists():
    for f in log_dir.glob("*.log"):
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for l in lines:
            if any(k in l.lower() for k in ("confirmation", "denying", "permission", "soft-deny", "applyproject")):
                print("LOG:", l)
