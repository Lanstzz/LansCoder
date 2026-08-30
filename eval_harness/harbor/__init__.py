"""Harbor regression/canary adapters for the unified evaluation harness.

Harbor is an optional, external benchmark runtime.  Keeping its adapters in
``eval_harness.harbor`` keeps Harbor's dependency and container lifecycle out
of LansCoder's core runtime and the offline golden gate.
"""
