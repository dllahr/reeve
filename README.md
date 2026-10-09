# Reeve

A tabletop-RPG referee server: event-sourced world state, role-based visibility
(DM / player / observer), undo/redo, and pluggable rulesets (AD&D 1e first).

Design and decisions: [DESIGN.md](DESIGN.md). Status: **build step 1 of 10 done** (core event store).

## Dev setup

```
conda activate reeve          # Python 3.12; created with: conda create -n reeve python=3.12
pip install -e '.[dev,server]'
pytest
```

`artifacts/` holds a snapshot of the original Claude-as-DM campaign (tools, logs, rulebooks).
It is reference material and test data only, and is git-ignored.
