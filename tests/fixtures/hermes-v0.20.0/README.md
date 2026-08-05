# Hermes Agent v0.20.0 patcher fixture

These files are pristine copies from release commit
`3c27eb6234bf91b8ceee9e9071591b31e9b148cb` (`v0.20.0`, 2026-08-03).

The Feishu adapter fixture is pinned at:

```text
plugins/platforms/feishu/adapter.py
sha256: 55cbb66fa60abdd3710a3476c197b31d46dc3ff13ec401c23557ee6465f96029
```

The patcher regression tests verify the exact release source before applying
hooks, after applying hooks, on an idempotent re-apply, and after removal.
