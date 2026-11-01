"""The apply engine's per-field groups (trinity-enterprise#1030).

Each module owns one group of manifest fields and exposes the three seams the
engine in `services/system_service.py` dispatches to: a read for the snapshot,
a pure plan over it, and the executor's writes. The engine itself stays where
it is; only the new groups land here.
"""
