# Security, API, supply-chain, and operations guidance

Local requirements and threat models are authoritative. Use these public
sources to identify missing scenarios and evidence; never claim formal
compliance from a source review or test run.

## Sources and application

- [NIST Secure Software Development Framework 1.1](https://csrc.nist.gov/pubs/sp/800/218/final)
  organizes secure development practices around preparing the organization,
  protecting software, producing well-secured software, and responding to
  vulnerabilities. Look for repository evidence relevant to its own lifecycle.
- [OWASP Application Security Verification Standard](https://owasp.org/www-project-application-security-verification-standard/)
  is an advisory catalog for web-application controls. Apply only relevant
  control areas and cite the repository's exposed attack surface.
- The [OWASP Web Security Testing Guide](https://wstg.owasp.org/) provides test
  ideas for web systems. Do not perform live offensive testing or contact
  external targets during a repository review.
- [SLSA specification 1.2](https://slsa.dev/spec/v1.2/) frames source, build,
  provenance, and dependency threats. Inspect claims and evidence without
  assigning a SLSA level unless the repository supplies complete attestations.
- [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html) defines HTTP semantics.
  Use it for method safety/idempotency, status, cache, and representation
  questions when the repository exposes HTTP.
- The [OpenAPI Specification](https://spec.openapis.org/oas/) is the reference
  for OpenAPI documents. Compare checked-in schemas with routing, validation,
  authentication, errors, and generated snapshots.
- Docker's [build best practices](https://docs.docker.com/build/building/best-practices/)
  informs image pinning, small contexts, reproducibility, privilege, and secret
  handling; repository deployment requirements take precedence.
- Google's SRE book chapter on [release engineering](https://sre.google/sre-book/release-engineering/)
  discusses reproducibility, automation, hermeticity, and deployment policy.
- [The Twelve-Factor App](https://12factor.net/) is optional operational design
  guidance, not a universal architectural requirement.

## Review lenses

- Trace authentication separately from authorization. Test default denial,
  object/tenant boundaries, role reductions, replay, expiry, and secret
  redaction where applicable.
- Compare API implementation, generated contract, examples, SDK assumptions,
  error bodies, pagination, idempotency, and compatibility guarantees.
- Inspect untrusted input boundaries: archive/path traversal, symlinks, URLs and
  redirects, SSRF and DNS rebinding, command construction, deserialization,
  uploads, size limits, and resource exhaustion.
- Follow durable writes through transactions, migrations, uniqueness,
  concurrency, retry, partial failure, backup, restore, and downgrade behavior.
- For dependency/build flows, distinguish exact locks and verified provenance
  from floating resolution. Check that secrets do not enter layers, logs,
  artifacts, fixtures, or generated metadata.
- Compare deployment diagrams and runbooks with actual networks, mounts,
  identities, capabilities, health/readiness, quotas, logging, rollout, and
  rollback behavior.

The critic's own command egress is a deliberate deployment risk. The managed
policy admits only `example.com`, `pypi.org`, `files.pythonhosted.org`,
`registry.npmjs.org`, `proxy.golang.org`, and `sum.golang.org` and reduces
local/private access, but its hostname-only filtering does not restrict scheme,
port, method, or payload for those services and cannot prevent source
exfiltration through an admitted endpoint. Do not add credentials to dependency
restores, and report this limitation in every run that enables network access.
OpenAI's current operational context is documented
in the [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [internet-access risk guidance](https://learn.chatgpt.com/docs/cloud/internet-access).
