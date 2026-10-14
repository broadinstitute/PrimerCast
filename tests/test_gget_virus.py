"""Tests for the gget virus wrapper (primercast.external.gget_virus)."""

import sys
import types

import pytest

from primercast.external import gget_virus as gv


@pytest.mark.parametrize("args,expected", [
    (["2697049", "--out", "o"], False),
    (["2697049", "--host", "human", "--nuc_completeness", "complete"], False),
    (["2697049", "--geographic_location", "USA"], True),
    (["2697049", "--min_release_date=2025-01-01"], True),
    (["2697049", "--max_collection_date", "2025-06-01"], True),
    (["2697049", "--min_release_date", "2025-01-01", "--lineage", "JN.1"], False),
])
def test_use_api_path(args, expected):
    assert gv.use_api_path(args) is expected


def _fake_gget(monkeypatch, has_skip_cache=True):
    calls = {}

    if has_skip_cache:
        def virus(virus, _skip_cache=False, **kw):
            calls["skip"] = _skip_cache
    else:
        def virus(virus, **kw):
            calls["skip"] = kw.get("_skip_cache", "absent")

    main_mod = types.ModuleType("gget.main")
    main_mod.virus = virus
    # Like gget, the CLI passes _skip_cache=False explicitly.
    main_mod.main = lambda: (calls.setdefault("argv", list(sys.argv)),
                             main_mod.virus("x", _skip_cache=False))
    pkg = types.ModuleType("gget")
    pkg.main = main_mod
    monkeypatch.setitem(sys.modules, "gget", pkg)
    monkeypatch.setitem(sys.modules, "gget.main", main_mod)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    return calls


def test_filtered_query_skips_bulk_package(monkeypatch):
    calls = _fake_gget(monkeypatch)
    gv.main(["2697049", "--geographic_location", "USA"])
    assert calls["argv"] == ["gget", "virus", "2697049", "--geographic_location", "USA"]
    assert calls["skip"] is True


def test_unfiltered_query_unchanged(monkeypatch):
    calls = _fake_gget(monkeypatch)
    gv.main(["2697049"])
    assert calls["skip"] is False


def test_older_gget_without_flag(monkeypatch):
    calls = _fake_gget(monkeypatch, has_skip_cache=False)
    gv.main(["2697049", "--min_release_date", "2025-01-01"])
    assert calls["skip"] is False
