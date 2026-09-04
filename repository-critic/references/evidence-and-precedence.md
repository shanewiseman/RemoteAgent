# Evidence and precedence

Use this note to turn a repository snapshot into an auditable review. It does
not override repository-local policy or the Repository Critic base contract.

## Authority order

Apply the most specific applicable authority, recording exceptions explicitly:

1. Normative repository requirements and specifications using terms such as
   MUST, SHALL, guaranteed, or supported.
2. Machine-readable public contracts: API/schema files, migrations, manifests,
   generated protocol snapshots, type declarations, and configuration schemas.
3. Accepted architectural decisions and operational/security runbooks.
4. User-facing behavior documentation and examples.
5. Tests, which prove sampled behavior but are not automatically the product
   specification.
6. Implementation, which shows actual behavior even when it contradicts prose.
7. Applicable external engineering guidance, used only as advisory context.

Resolve conflicting documents by specificity and clearly declared normative
status, not by whichever file is newest. When the repository gives no priority,
mark the conflict and the chosen interpretation as a limitation.

## Traceability matrix

Create one row per material, testable claim:

| Field | Meaning |
| --- | --- |
| Claim ID | Stable review-local identifier, such as `DOC-001` |
| Claim | One falsifiable requirement, not a paragraph of mixed promises |
| Authority | Exact path/section or machine-readable contract |
| Implementation | Path, symbol, and line that implements or contradicts it |
| Test | Test path/name or an explicit absence after search |
| Status | `verified`, `partial`, `contradicted`, or `unverifiable` |

`Verified` requires implementation and relevant test evidence. `Partial` means
some stated cases or qualities are absent. `Contradicted` requires positive
counter-evidence. `Unverifiable` means the available snapshot or safe runtime
cannot establish the claim; it is not shorthand for false.

## Findings

Every finding must be independently actionable:

- **P0**: demonstrated or highly credible immediate security compromise,
  irreversible data loss, or unusable critical service.
- **P1**: likely serious contract violation, security boundary gap, corruption,
  or failure in a critical supported workflow.
- **P2**: material maintainability, reliability, coverage, performance, or
  documentation defect without immediate critical impact.
- **P3**: bounded improvement with low operational impact.

Confidence is separate from priority. Use high confidence for direct executable
or machine-contract evidence, medium for strong multi-source inference, and low
when applicability or reachability is uncertain. Do not inflate priority to
compensate for low confidence.

Evidence references should be repository-relative and include the tightest
useful line. Never publish secrets or entire large files. A recommendation says
what invariant to establish and how to verify it; it need not prescribe a patch.

## Snapshot discipline

The review subject is the complete prepared snapshot. Commit ID, dirty state,
source digest, tool versions, executed argv, limits, failures, and exclusions
are provenance. They are never used to manufacture PR context. Generated files,
vendored code, fixtures, and migrations remain in scope but may have different
style and coverage expectations; state each exclusion rather than silently
discarding it.
