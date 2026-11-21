# Isolated job-site fixture

A source-only subset of the local simulator, frozen for AutoHunt evaluation.
Contains the listing/detail pages and HTTP contracts used by production adapters.
No account inventory, seeds, credentials, profiles, databases, or runtime history.
The harness creates fictional records in a fresh SQLite database on a random loopback port.
Header identity and displayed demo MFA are test conveniences, not production authentication.
Start only through `evaluation.benchmark.execution.IsolatedSite` / `site_worker`.
The separate full `mock-site/` simulator is neither required nor modified.
