# Repository guidance

Pushes from clud sessions are gated by `pr_checks` (talos-hq/clud#2720): run `clud pr-check` before pushing; it runs `uv run --frozen python ci.py fmt lint types` (Ruff formatting and lint checks, plus basedpyright type checks).
