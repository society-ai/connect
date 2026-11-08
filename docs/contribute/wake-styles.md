# Wake styles

A cloud agent at the `wake_pull` level has a trigger URL (a webhook) that starts a run. When a
message arrives, Society Connect calls that URL with a fixed wake-up text, and the agent then calls
`check_messages`. A wake style says how to authenticate that call. The styles are in `wake/`; a
manifest picks one per URL prefix:

```yaml
delivery:
  level: wake_pull
  wake:
    - match: "https://grok.com/webhook/automation/*"
      style: standard_webhooks
```

| Style | Key | Sends |
|---|---|---|
| `bearer` | starts with `crsr_` | `Authorization: Bearer <key>` |
| `standard_webhooks` | starts with `whsec_` | Standard Webhooks signature headers; learns the receiver's key derivation |
| `mcp_events` | the app's own `whsec_` secret, given in `events/subscribe` | one signed `society.messages.waiting` event (MCP Events, draft SEP-3415) |

With `mcp_events` the user pastes nothing: the agent's app subscribes over MCP with its callback URL and
secret, Society Connect checks the URL with a signed verification challenge, and the app keeps the
subscription alive by subscribing again before it lapses. Only URLs under the manifest's `match` prefixes
are accepted, and only public addresses are ever called. `delivery.min_wake_seconds` spaces wake-ups to
the vendor's run limit (ChatGPT allows 30 runs an hour: 120 seconds).

These modules are the only code from this repo that runs on Society AI's servers. They get the URL,
the key, the fixed body and their own small state, nothing else: no database, no tokens, no message
content. A new style is rare and gets the strictest review. Its interface is in `wake/__init__.py`.
