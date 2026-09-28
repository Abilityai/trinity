"""Test helper for the #1482 parameter objects.

The four widest writers now take one object (`result=` / `fields=` / `source=`)
instead of a dozen keywords. `flat_kwargs(call)` merges that object's fields
back into the call's keyword dict, so an assertion like
`flat_kwargs(mock.update_execution_status.call_args)["error"]` reads the value
the caller sent without every test re-deriving which object carried it.
"""
from dataclasses import asdict, is_dataclass

_OBJECT_KWARGS = ("result", "fields", "source")


def flat_kwargs(call) -> dict:
    """Keyword args of a mock `call`, with a parameter object's fields inlined."""
    kw = dict(call.kwargs)
    for name in _OBJECT_KWARGS:
        obj = kw.get(name)
        if is_dataclass(obj):
            del kw[name]
            kw.update(asdict(obj))
    return kw


def object_keywords(call_node) -> list:
    """AST twin of `flat_kwargs`: a call node's keywords, with those written
    inside a `result=` / `fields=` / `source=` parameter-object constructor
    inlined — for source guards that check which fields a writer passes."""
    import ast

    kws = []
    for kw in call_node.keywords:
        if kw.arg in _OBJECT_KWARGS and isinstance(kw.value, ast.Call):
            kws.extend(kw.value.keywords)
        else:
            kws.append(kw)
    return kws
