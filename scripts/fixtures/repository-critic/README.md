# Repository Critic Smoke Fixture

This repository is intentionally small and internally inconsistent. It exists
only to exercise the live repository-critic workflow.

## Authorization contract

`RA_CRITIC_DENY_NON_ADMIN`: `authorize_sensitive_operation` MUST return `true`
only for the exact token `fixture-admin`. Every other token, including an empty
value, MUST be denied.

## Supported test command

Restore the declared Python dependency, then run:

```sh
python -m unittest discover -s tests -v
```

The dependency declaration is intentionally unlocked. A reviewer that resolves
it must record the exact installed version and source in its provenance output.

`scripts/bootstrap.sh` is a deployment lifecycle helper, not part of the test
workflow. It must run only when the caller explicitly authorizes lifecycle
scripts.
