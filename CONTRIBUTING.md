# Contributing

Thank you. Agents and people contribute the same way; agents should read [AGENTS.md](AGENTS.md)
first.

1. Find or open an issue (use the forms: new agent type, bug, setup help, capability report). Found a
   problem but not fixing it yourself? Flagging it is a contribution too.
2. Make your change. Most contributions are a whole `agents/<id>/` folder:
   [docs/contribute/adding-an-agent-type.md](docs/contribute/adding-an-agent-type.md).
3. Run `./connect dev check` until it passes.
4. Open a pull request and fill in the template: the agent type, what you tested and how, the
   evidence, and whether you changed what a runner may do on users' machines.

**Who answers for a pull request:** the owner of the GitHub account that opens it. There is no
sign-off step: GitHub's terms license what you contribute under this repo's MIT license.

**Review:** the Society AI maintainer agent reviews every pull request against
[docs/review.md](docs/review.md), local adapters included, because users download them and trust
that we checked them. Changes to `core/`, `runners/`, `wake/` and `schema/` get a second review.

**Releases:** tagged and signed by Society AI, with Lior's go. Your type reaches users with the next
release, labelled Community, and appears in the Society AI app when the release is pinned there.
