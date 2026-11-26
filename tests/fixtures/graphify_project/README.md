# A real graphify graph

`graphify-out/graph.json` is graphify 0.8.33's own output for the three source files
here, produced with `graphify update .` (code extraction only, no LLM) on 2026-11-26.
It is not edited: the ownership tests (tests/test_refresh.py) check that slurp never
changes a byte of it, whatever happens to the sources.
