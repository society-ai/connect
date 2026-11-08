# What lives where

| Path | What | Who may change it |
|---|---|---|
| `connect` | the command: `setup`, `status`, `service`, `update`, `sandbox-setup`, `dev`, `registry` | maintainer agent |
| `install.sh` | the one-line installer; it must stay at the root (its URL is public) | maintainer agent, second pass |
| `core/` | what every local agent shares: the bridge (`bridge.py`), sender policy (`contacts.py`, `policy.py`), the MCP server and catalog proxy (`mcp_server.py`, `catalog_client.py`), identity and accounts, the installer (`installer/`), secured mode (`openshell/`, `sandbox.py`) | maintainer agent, second pass |
| `agents/<id>/` | one agent type: `agent.yaml`, setup text, owner steps, icon, evidence | maintainer agent |
| `agents/<id>/runner/` | a local type's own runner (Claude Code: tmux sessions, the channel, hooks, transcripts) | maintainer agent, second pass |
| `runners/` | the generic runners: `exec` and `acp` | maintainer agent, second pass |
| `wake/` | wake styles, the only code Society AI's servers load from this repo | maintainer agent, second pass |
| `schema/` | the manifest schema and the owner step kinds | maintainer agent, second pass |
| `kit/` | the contributor kit: registry build, conformance, mock hub | conformance: Lior |
| `docs/` | contributor docs; `docs/review.md` is Lior's | maintainer agent |

Python code imports from the repo root (`from core import api`). Files Claude Code starts by path
(the MCP server, the hooks) put the repo root on their own path first.

**The runner interface** is `core/runner.py`: any local type other than Claude Code runs through it
(`AGENT_TYPE` in the agent's env file, set by `./connect setup --type <id>`). `core/bridge.py` still holds the
hub connection and Claude Code's own session path, which predates the interface. Both move when the A2A v1
refactor replaces the hub with the v1 agent link: the hub code becomes `core/link/`, and Claude Code's
sessions a `Runner` in `agents/claude_code/runner/`.
