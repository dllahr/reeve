# Reeve

A tabletop-RPG referee server: event-sourced world state, role-based visibility
(DM / player / observer), undo/redo, and pluggable rulesets (AD&D 1e first).

Design and decisions: [DESIGN.md](DESIGN.md). Status: **build steps 1-2 of 10 done** (core event store; roll registry and undo/redo).

## Authorship

This project is written by Claude (Anthropic's AI model, via Claude Code) under the guidance of Dave Lahr,
who sets the direction, makes the design decisions, and reviews the work. The design conversation and its
resulting decisions are recorded in [DESIGN.md](DESIGN.md).

## Dev setup

```
conda activate reeve          # Python 3.12; created with: conda create -n reeve python=3.12
pip install -e '.[dev,server]'
pytest
```

`artifacts/` holds a snapshot of the original Claude-as-DM campaign (tools, logs, rulebooks).
It is reference material and test data only, and is git-ignored.
