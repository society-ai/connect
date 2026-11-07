# The registry

`./connect registry build --version <v>` reads every `agents/*/` folder (not the `_template` ones)
and writes `registry.json`: each type's manifest with its setup file, owner step texts and icon
inlined, a content hash per type, and `core_rules`. Verified types come first, generic types last.

Society Connect and the Society AI app each load a pinned copy. Moving the pin is a deploy, with
Lior's go. They never read this repo's main branch. A type can be switched off at once on the server
(kill switch) without a release.
