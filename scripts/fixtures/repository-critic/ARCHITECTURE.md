# Architecture Contract

Authorization policy belongs in a dedicated `review_target/policy.py` domain
module. Adapters may call that policy but must not implement or weaken the
decision themselves. This boundary is intended to keep deny-by-default behavior
independent of transport and presentation code.
