---
name: artifact-publishing
description: Save caller-requested deliverables where the RemoteAgent router can expose them as MCP resources.
---

# Artifact publishing

When the caller asks for a downloadable or reusable file, write it beneath the
directory named by `REMOTEAGENT_ARTIFACTS` (normally `/workspace/artifacts`).

- Use descriptive, portable file names without path traversal components.
- Never place credentials, authentication caches, or environment dumps there.
- Mention the saved file in the final response; the router registers files from
  this directory as MCP resources after the turn completes.
- Keep temporary and intermediate files outside the artifact directory.

