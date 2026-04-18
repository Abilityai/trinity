"""Local conftest — isolates tests/git-sync/ from the parent tests/conftest.py.

The parent conftest authenticates against a running backend (401 against
localhost:8000) which is noise for these pure-function tests. By defining
this local conftest, pytest's root-conftest discovery is satisfied at this
level and we avoid pulling in the parent's fixtures.
"""
