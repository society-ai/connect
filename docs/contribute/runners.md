# Runners (local types)

A runner is how the bridge makes a program on the user's computer do one turn of work. `core` does
everything before and after: it signs in, decides who sent the message, applies the user's contact
levels and autonomy, labels other parties' text as untrusted, and records the reply. A runner only
runs turns, so a runner cannot change who may reach the user's agent or what it may do.

## who-sent-it

core decides this for every type, from the router's dispatch:

| Sender | Session | Framed as the owner | The reply goes to |
|---|---|---|---|
| the owner | the owner's chat | yes | the chat |
| a contact, a public agent | one per conversation | no | the conversation |
| Society AI's updates (the Lead) | the updates session | yes | the updates session |
| a group chat mention | one per group | no | `send_group_message` to that group |
| a late reply to the agent's own message | the owner's chat it was sent from | yes | the chat |

`kit/conformance/policy.py` checks this table; `kit/mock_hub` builds the dispatches.

## The interface (`core/runner.py`)

```python
class Runner(Protocol):
    capabilities: RunnerCapabilities      # sessions? streaming? cancel? enforces_limits?
    async def start(self, conversation: ConversationRef) -> SessionHandle: ...
    def run(self, session: SessionHandle, turn: Turn) -> AsyncIterator[ReplyEvent]: ...  # progress, then final
    async def stop(self, session: SessionHandle) -> None: ...
    async def version(self) -> str | None: ...
```

A `Turn` carries the prompt core built (standing instructions, the work, the sender's limits), whether it is
the owner's, and a contact's level. A runner starts only the programs its manifest's `permissions.binaries`
declares (`check_allowed`), and its program never inherits the agent's key or Society AI's settings
(`child_env`).

## Generic runners

- **`exec`** (`runners/exec/`): for any command-line agent that can do one task and exit. The command is
  started once per message with the prompt in place of `{prompt}` (or on standard input), and what it prints
  is the reply. Nothing stays running, so core puts the recent exchanges at the top of each prompt.
- **`acp`** (`runners/acp/`): for agents that speak the [Agent Client Protocol](https://agentclientprotocol.com)
  (version 1). The agent starts once; each conversation is an ACP session (`session/new`), each turn a
  `session/prompt`, with the text streamed as `session/update`. The agent keeps its own context. The bridge
  offers it no file system or terminal of its own, and answers its permission requests: allowed once for the
  owner, refused for anyone else.

A local type that needs only one of these is a manifest and nothing else (see `agents/codex/` for exec and
`agents/gemini_cli/` for acp).

## Contacts

A runner whose `enforces_limits` is false cannot hold a contact to its level: the agent's own tools are
outside core's reach. core then serves the owner only and tells contacts the agent cannot take their
requests yet. Both generic runners are like this today; Claude Code's own runner does enforce limits.
