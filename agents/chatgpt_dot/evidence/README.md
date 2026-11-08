# Evidence

No test with a real ChatGPT Dot yet (2026-11-08). Built from OpenAI's plugin guide and the MCP Events
draft (SEP-3415). Before this type is available, a run with a real Dot account should show: the plugin
connects and is matched to chatgpt_dot (not generic_mcp), the Dot sets itself up, an events/subscribe
call passes verification (and from which callback host, so the wake URL prefixes in agent.yaml are
right), a message wakes it, and contacts' messages are handled at their level, plus two `connector_check`
reports (the second after a `send_test` round trip, `ok: true`). No personal data.
