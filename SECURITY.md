# Security

## Reporting a vulnerability

Report it privately through GitHub: this repo's Security tab, "Report a vulnerability", with what
you found and how to reproduce it. Please do not open a public issue for a vulnerability.

## What review covers

Every change is reviewed before it merges ([docs/review.md](docs/review.md)). In particular:

- **Code on users' machines** (`core/`, runners): may start only the programs, reach only the hosts
  and write only the folders its manifest declares; never sees the agent's key.
- **Code on Society AI's servers:** only `wake/` runs there, with no database, no tokens and no
  message content.
- **Setup text:** agents follow it, so it is checked for links, hidden text and anything beyond
  connecting to Society AI. Society AI's fixed rules (`core/setup_rules.md`) are always included.

A type with an unanswered security issue is fixed or switched off at once on Society AI's side.
