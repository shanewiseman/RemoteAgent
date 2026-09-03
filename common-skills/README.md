# Common skills

This directory seeds the `remoteagent-common-skills` Docker volume. Every agent
mounts that volume at `/opt/remoteagent/skills`, and the runtime links it into
`$CODEX_HOME/skills`.

Add each skill as its own directory containing a `SKILL.md`. Run
`scripts/remotectl skills sync` after changing checked-in skills on an existing
deployment; initial volume creation is populated from the agent base image.

