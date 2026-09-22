# Common skills

This directory seeds the `remoteagent-common-skills` Docker volume. Every agent
mounts that volume read-only at `/opt/remoteagent/skills`. The runtime links each
common skill into a writable `$CODEX_HOME/skills` directory, leaving its
`.system` directory available for Codex's bundled skills. Existing whole-directory
links to the configured shared skills path are migrated automatically.

Add each skill as its own directory containing a `SKILL.md`. Run
`scripts/remotectl skills sync` after changing checked-in skills on an existing
deployment; initial volume creation is populated from the agent base image.
On the next runner start, the runtime removes links to common skills that are no
longer present. It preserves custom skills and refuses name conflicts.
