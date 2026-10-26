# Assistant foundation release

Integrate committed foundation 1c794ef and ReAct 9ba8275 in an independent clone of acceptance d8c73a3. Keep ReAct loop untouched. Expose a trusted read-only diagnostic skill catalog including required capabilities and explicit experimental opt-in. Provide an explicit diagnostic parameter schema (including novel_ids arrays) rather than relying on union annotation inference. Diagnosis is audit-only; unknown responsibility/permission rules produce clarification questions and confirmation-required planning proposals, never automatic apply.

Acceptance: scoped discovery and diagnostic tests; existing planning/foundation and ReAct/claim/read-only tests; frontend build; real Assistant /qa selects diagnostic tools, observes output and answers with trace/source IDs on an isolated SQLite backup. Preserve source DB hashes/stat and dirty source files. Isolated ports must be checked before launch. No distillation or production service restart.
