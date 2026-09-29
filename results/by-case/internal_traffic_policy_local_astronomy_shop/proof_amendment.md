# Post-run proof schema amendment

The live pre-agent check passed and was saved before Assistant V3 started. After teardown, the scorecard validator rejected the proof because a `ready_data_limited` record omitted the required `causal.remedy` text. The verifier and this saved proof were amended only to add that explanatory remedy. Observed status, source/Splunk counts, window, run ID, answer, trace, and benchmark judge result were not changed. The existing run was then checkpointed; no agent or simulation retry was performed.
