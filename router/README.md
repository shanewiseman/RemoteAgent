# RemoteAgent router

Python package for the root MCP/HTTP router. The production entry point is
`remoteagent.app:create_app`; tests use the same services with a deterministic
in-process agent runtime. Release 0.3 adds staged file/archive/public-Git
conversation companions while preserving artifacts as the only downloadable
agent output surface.
