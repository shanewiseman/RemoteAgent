# Architecture and quality guidance

Repository-local architecture decisions and language/style tooling are
authoritative. Use these sources to ask disciplined questions when local
guidance is missing, not as a universal checklist or certification standard.

## Sources

- [Guide to the Software Engineering Body of Knowledge (SWEBOK v4)](https://www.computer.org/education/bodies-of-knowledge/software-engineering/v4)
  provides a public map of software requirements, design, construction,
  testing, maintenance, configuration management, quality, security, and
  professional practice.
- [ISO/IEC 25010:2023 product quality model](https://www.iso.org/standard/78176.html)
  names product-quality concerns such as functional suitability, reliability,
  performance efficiency, compatibility, interaction capability, security,
  maintainability, flexibility, and safety. The public abstract is context only;
  do not reproduce the standard or claim conformance.
- [ISO/IEC/IEEE 42010:2022](https://www.iso.org/standard/74393.html) frames
  architecture descriptions around stakeholders, concerns, viewpoints, views,
  and decisions. Use the public description only; do not claim certification.
- The SEI's [Views and Beyond approach](https://insights.sei.cmu.edu/library/views-and-beyond-the-sei-approach-for-architecture-documentation/)
  emphasizes documenting structures relevant to stakeholder concerns.
- The SEI [Architecture Tradeoff Analysis Method](https://sei.cmu.edu/library/atam-method-for-architecture-evaluation/)
  connects quality-attribute scenarios to architectural decisions, risks,
  sensitivity points, and tradeoffs.
- The [C4 model](https://c4model.com/) offers a lightweight vocabulary for
  system context, containers, components, and code diagrams.
- Google's [review guidance on what to look for](https://google.github.io/eng-practices/review/reviewer/looking-for.html)
  is useful advisory material for design, functionality, complexity, tests,
  naming, comments, style, documentation, and consistency. This critic applies
  those lenses to a repository, not to a proposed change.

## Review questions

- Do module and service boundaries match documented ownership and deployment
  boundaries? Are dependencies directed intentionally, with cycles explained?
- Are public contracts isolated from storage, framework, and transport details
  enough to evolve safely?
- Do error, retry, concurrency, and cancellation paths preserve documented
  invariants? Are idempotency and consistency assumptions visible?
- Are authentication, authorization, data classification, and trust boundaries
  enforced at the narrowest appropriate layer?
- Do migrations and compatibility paths match the promised upgrade/rollback
  model? Is durable state owned by a documented component?
- Can operators observe and recover the quality attributes the repository
  claims: availability, latency, durability, integrity, and security?
- Does repeated implementation style follow checked-in formatters, linters,
  type systems, and neighboring idioms? Flag a departure only when it creates
  inconsistency, ambiguity, coupling, defect risk, or real maintenance cost.

Avoid generic demands for layers, abstractions, patterns, microservices, or C4
diagrams. Architecture is fit for documented concerns, not conformity to a
favorite shape. When recommending a change, name the quality scenario it
improves and the tradeoff it introduces.
