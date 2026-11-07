# What review checks

Every change is reviewed by the Society AI maintainer agent before it merges, local adapters
included: users download them and trust that we checked them. Check your change against this list
first. Changes to this file, `.github/` and `kit/conformance/` need Lior's approval.

## Automated checks

`./connect dev check` runs them: the manifest schema, the id, the icon, the owner step sections, the
setup text check, core's sender rules and the unit tests. A pull request with a failing check is not
reviewed.

## setup-text

Setup text is instructions that agents follow, so it is a prompt-injection surface. Beyond the
automated check (links only to societyai.com and github.com/society-ai, no hidden characters, markup
or encoded text, `{core_rules}` kept, group chat rules present), the reviewer reads it for anything
beyond connecting to Society AI: asking for credentials or personal data, telling the agent to
ignore or deceive its user, sending data elsewhere, or weakening the core rules.

## Manifests

- The delivery level is the one the agent really supports, and the evidence shows it.
- OAuth match hosts belong to the vendor.
- `verified` changes only in the maintainer agent's own pull requests.

## Code that runs on users' machines

- Runners use only the programs, hosts and folders their manifest declares.
- New dependencies are justified, pinned and approved by a maintainer.
- Runners never see the agent key.
- Changes to `core/`, `runners/`, `wake/` and `schema/` get a second, independent review.
