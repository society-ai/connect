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

These modules are the only code from this repo that runs on Society AI's servers. They get the URL,
the key, the fixed body and their own small state, nothing else: no database, no tokens, no message
content. A new style is rare and gets the strictest review. Its interface is in `wake/__init__.py`.
