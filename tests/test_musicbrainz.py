"""Offline tests for recon_musicbrainz.py (the MusicBrainz API is mocked)."""
import json
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recon_musicbrainz as mb  # noqa: E402

FAKE_RELEASE_SEARCH = {"releases": [
    {"id": "rel-1111", "title": "Their Greatest Yiddish Hits", "score": 100,
     "date": "1962", "country": "US", "barcode": "0",
     "artist-credit": [{"name": "The Barry Sisters"}],
     "label-info": [{"catalog-number": "R 25156", "label": {"name": "Roulette"}}],
     "release-group": {"id": "rg-333"}},
    {"id": "rel-2222", "title": "Side By Side", "score": 70,
     "date": "1959", "country": "US",
     "artist-credit": [{"name": "The Barry Sisters"}],
     "label-info": [{"catalog-number": "R 25080", "label": {"name": "Roulette"}}]},
]}

FAKE_RELEASE_LOOKUP = {
    "id": "rel-1111", "title": "Their Greatest Yiddish Hits",
    "date": "1962", "country": "US", "barcode": "0", "status": "Official",
    "packaging": "Cardboard Sleeve",
    "artist-credit": [{"name": "The Barry Sisters"}],
    "release-group": {"id": "rg-333"},
    "label-info": [{"catalog-number": "R 25156", "label": {"name": "Roulette"}}],
    "media": [{"format": "Vinyl", "track-count": 1,
               "tracks": [{"number": "A1", "title": "Chiribim Chiribom",
                           "length": 165000}]}],
}

FAKE_LABEL_SEARCH = {"labels": [
    {"id": "lab-9", "name": "Roulette", "type": "Original Production",
     "area": {"name": "United States"}, "score": 100},
]}

FAKE_LABEL_LOOKUP = {
    "id": "lab-9", "name": "Roulette", "type": "Original Production",
    "label-code": 12345, "country": "US", "area": {"name": "United States"},
    "life-span": {"begin": "1957"},
}


@pytest.fixture()
def client():
    def fake_get(path, params=None):
        if path == "/release":
            return FAKE_RELEASE_SEARCH
        if path == "/label":
            return FAKE_LABEL_SEARCH
        if path.startswith("/release/"):
            return FAKE_RELEASE_LOOKUP
        if path.startswith("/label/"):
            return FAKE_LABEL_LOOKUP
        return None

    with mock.patch.object(mb, "mb_get", side_effect=fake_get):
        mb._entity_cache.clear()
        yield mb.app.test_client()


def test_manifest_declares_all_entity_types(client):
    m = client.get("/reconcile").get_json()
    ids = [t["id"] for t in m["defaultTypes"]]
    assert {"release", "release-group", "artist", "label", "recording"} <= set(ids)
    assert m["suggest"]["property"]["service_path"] == "/reconcile/suggest/property"


def test_release_reconcile_top_match(client):
    queries = {"q0": {
        "query": "Greatest Yiddish Hits",
        "type": "release",
        "properties": [
            {"pid": "artist", "v": "Barry Sisters"},
            {"pid": "catno", "v": "R25156"},   # no space — normalization test
            {"pid": "year", "v": "1962"},
        ],
    }}
    r = client.post("/reconcile", data={"queries": json.dumps(queries)})
    results = r.get_json()["q0"]["result"]
    assert results[0]["id"] == "release/rel-1111"
    assert results[0]["match"] is True
    assert results[0]["type"][0]["id"] == "release"


def test_build_query_uses_lucene_fields():
    bound = {"artist": "Barry Sisters", "year": "1962", "catno": "R 25156",
             "country": "US", "label": "", "format": "", "barcode": ""}
    q = mb.build_query("release", "Greatest Hits", bound)
    assert 'release:"Greatest Hits"' in q
    assert 'artistname:"Barry Sisters"' in q
    assert "date:1962*" in q
    assert 'catno:"R 25156"' in q
    assert "country:US" in q


def test_build_query_ignores_filters_invalid_for_entity():
    # an artist search has no catno/label fields, so they must be dropped
    bound = {"catno": "R-1", "label": "Roulette", "year": "", "artist": "",
             "country": "US", "format": "", "barcode": ""}
    q = mb.build_query("artist", "The Barry Sisters", bound)
    assert 'artist:"The Barry Sisters"' in q
    assert "catno" not in q and "label:" not in q
    assert "country:US" in q  # country IS valid for artist


def test_search_routes_to_entity_endpoint():
    seen = {}

    def fake_get(path, params=None):
        seen["path"] = path
        seen["params"] = dict(params or {})
        return FAKE_LABEL_SEARCH

    with mock.patch.object(mb, "mb_get", side_effect=fake_get):
        mb._entity_cache.clear()
        client = mb.app.test_client()
        q = {"q0": {"query": "Roulette", "type": "label"}}
        r = client.post("/reconcile", data={"queries": json.dumps(q)})
    assert seen["path"] == "/label"
    assert 'label:"Roulette"' in seen["params"]["query"]
    top = r.get_json()["q0"]["result"][0]
    assert top["id"] == "label/lab-9"
    assert top["match"] is True


def test_property_suggest(client):
    r = client.get("/reconcile/suggest/property?prefix=cat").get_json()
    assert [p["id"] for p in r["result"]] == ["catno"]
    r = client.get("/reconcile/suggest/property?prefix=").get_json()
    assert "artist" in [p["id"] for p in r["result"]]


def test_release_data_extension(client):
    extend = {"ids": ["release/rel-1111"],
              "properties": [{"id": "catno"}, {"id": "labels"},
                             {"id": "release_group_mbid"}, {"id": "track_count"},
                             {"id": "musicbrainz_url"}, {"id": "tracklist_json"}]}
    r = client.post("/reconcile", data={"extend": json.dumps(extend)})
    body = r.get_json()
    rows = body["rows"]["release/rel-1111"]
    assert rows["catno"][0]["str"] == "R 25156"
    assert rows["labels"][0]["str"] == "Roulette"
    assert rows["release_group_mbid"][0]["str"] == "rg-333"
    assert rows["track_count"][0]["str"] == "1"
    assert rows["musicbrainz_url"][0]["str"] == "https://musicbrainz.org/release/rel-1111"
    assert json.loads(rows["tracklist_json"][0]["str"])[0]["position"] == "A1"
    assert "catno" in [m["id"] for m in body["meta"]]


def test_label_data_extension(client):
    # propose_properties returns label-specific fields
    props = client.get("/reconcile/propose_properties?type=label").get_json()
    assert "label_code" in [p["id"] for p in props["properties"]]
    extend = {"ids": ["label/lab-9"],
              "properties": [{"id": "label_code"}, {"id": "area"}, {"id": "begin"}]}
    r = client.post("/reconcile", data={"extend": json.dumps(extend)})
    rows = r.get_json()["rows"]["label/lab-9"]
    assert rows["label_code"][0]["str"] == "12345"
    assert rows["area"][0]["str"] == "United States"
    assert rows["begin"][0]["str"] == "1957"


def test_loose_retry_when_strict_filters_miss():
    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params or {}))
        # strict query (with catno) misses; loose query (artist only) hits
        if "catno" in (params or {}).get("query", ""):
            return {"releases": []}
        return FAKE_RELEASE_SEARCH

    with mock.patch.object(mb, "mb_get", side_effect=fake_get):
        mb._entity_cache.clear()
        client = mb.app.test_client()
        q = {"q0": {"query": "Greatest Yiddish Hits", "type": "release",
                    "properties": [{"pid": "artist", "v": "Barry Sisters"},
                                   {"pid": "catno", "v": "NOPE-999"}]}}
        r = client.post("/reconcile", data={"queries": json.dumps(q)})
    assert len(calls) == 2  # strict then loose
    top = r.get_json()["q0"]["result"][0]
    assert top["id"] == "release/rel-1111"


def test_ms_to_mmss():
    assert mb.ms_to_mmss(165000) == "2:45"
    assert mb.ms_to_mmss(None) == ""
