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

The runner interface and the `exec` and `acp` generic runners arrive with step C6.
