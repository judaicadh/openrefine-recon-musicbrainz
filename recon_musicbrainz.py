"""
recon_musicbrainz.py — An OpenRefine reconciliation service backed by the
MusicBrainz API.

Reconcile music-catalog values against MusicBrainz, with supporting columns
bound as structured Lucene search fields (not just fuzzy hints):

    artist, year, label, catno, country, format, barcode

Types:
    release (default)  — a specific release (pressing/edition)
    release-group      — the abstract album grouping all its releases
    artist             — a performer, band, or composer
    label              — a record label / imprint
    recording          — a single recorded track

Setup:
    pip install flask rapidfuzz requests
    # No API key needed. MusicBrainz only asks for a descriptive User-Agent
    # with a contact URL/email — set one so they can reach you if needed:
    export MUSICBRAINZ_CONTACT="you@example.org"      # optional
    python recon_musicbrainz.py
    # OpenRefine -> Reconcile -> Add standard service ->
    #   http://localhost:8767/reconcile

In the reconciliation dialog, bind columns to properties by typing the
property name (artist, year, label, catno, ...) in the "As property" box.
These become real MusicBrainz search fields, narrowing the candidate pool
itself rather than only re-ranking it.

Data extension (Add columns from reconciled values) offers entity-specific
fields — e.g. for a release: mbid, release_group_mbid, artist, date,
country, labels, catno, barcode, formats, status, track_count,
musicbrainz_url, tracklist_json.

Respects the MusicBrainz rate limit (~1 request/second) with automatic
throttling.
"""

import json
import logging
import os
import re
import threading
import time
import unicodedata

import requests as rq
from flask import Flask, request, jsonify
from rapidfuzz import fuzz

HOST = "0.0.0.0"
PORT = 8767
SERVICE_NAME = "MusicBrainz"
CONTACT = os.environ.get("MUSICBRAINZ_CONTACT", "https://shira.wikibase.cloud")
USER_AGENT = f"ShiraOpenRefineRecon/1.0 ( {CONTACT} )"

API = "https://musicbrainz.org/ws/2"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("recon")

app = Flask(__name__)
_entity_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# --- polite rate limiting -------------------------------------------------
# MusicBrainz asks for no more than ~1 request/second from a single source.
_rate_lock = threading.Lock()
_last_request = [0.0]
MIN_INTERVAL = 1.1


def mb_get(path: str, params: dict | None = None) -> dict | None:
    with _rate_lock:
        wait = MIN_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = dict(params or {})
    params["fmt"] = "json"
    try:
        r = rq.get(f"{API}{path}", params=params, headers=headers, timeout=20)
        if r.status_code == 503:  # rate limited
            log.warning("MusicBrainz 503 rate limited; sleeping 2s")
            time.sleep(2)
            r = rq.get(f"{API}{path}", params=params, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("MusicBrainz request FAILED: %s %s", path, params)
        return None


# --- normalization & scoring ----------------------------------------------

def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    for pat, rep in [
        (r"tsch|tsh|tch", "ch"),
        (r"sch", "sh"),
        (r"kh|ch(?=[aou])", "h"),
        (r"oi", "oy"),
        (r"ie", "i"),
        (r"w", "v"),
    ]:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def norm_catno(s: str) -> str:
    """'R 25156', 'R-25156', 'r25156' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# --- entity model -----------------------------------------------------------

# Entities whose primary display field is "title" + an artist credit.
TITLE_ENTITIES = {"release", "release-group", "recording"}

ENTITY_LABELS = {
    "release": "Release",
    "release-group": "Release group",
    "artist": "Artist",
    "label": "Label",
    "recording": "Recording",
}

# Lucene field used for the reconciled cell value itself, per entity.
ENTITY_QUERY_FIELD = {
    "release": "release",
    "release-group": "releasegroup",
    "artist": "artist",
    "label": "label",
    "recording": "recording",
}

# JSON key holding the result list in a search response, per entity.
RESULT_KEY = {
    "release": "releases",
    "release-group": "release-groups",
    "artist": "artists",
    "label": "labels",
    "recording": "recordings",
}

# inc= subqueries needed at lookup time to populate the extension fields.
LOOKUP_INC = {
    "release": "artist-credits labels release-groups media recordings",
    "release-group": "artist-credits",
    "artist": "",
    "label": "",
    "recording": "artist-credits isrcs",
}

# Bound properties -> the Lucene field name valid for each entity. A property
# absent from an entity's map is simply ignored for that entity's search.
ENTITY_FILTER_FIELDS = {
    "release": {"artist": "artistname", "year": "date", "label": "label",
                "catno": "catno", "country": "country", "format": "format",
                "barcode": "barcode"},
    "release-group": {"artist": "artistname"},
    "recording": {"artist": "artistname", "year": "date",
                  "country": "country", "format": "format"},
    "artist": {"country": "country"},
    "label": {"country": "country"},
}

# Properties bindable in the reconciliation dialog ("As property").
FILTER_PROPS = ["artist", "year", "label", "catno", "country", "format", "barcode"]
FILTER_PROP_META = [
    {"id": "artist", "name": "Artist"},
    {"id": "year", "name": "Year"},
    {"id": "label", "name": "Label"},
    {"id": "catno", "name": "Catalog number"},
    {"id": "country", "name": "Country (ISO code, e.g. US)"},
    {"id": "format", "name": "Format (e.g. Vinyl, CD)"},
    {"id": "barcode", "name": "Barcode"},
]


def prop_value(props: list, pid: str) -> str:
    for p in props or []:
        if p.get("pid") == pid:
            v = p.get("v")
            if isinstance(v, list):
                v = " ".join(str(x) for x in v)
            return str(v or "").strip()
    return ""


def lucene_phrase(v: str) -> str:
    """Quote a value as a Lucene phrase, escaping the two characters that are
    still special inside quotes."""
    v = v.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{v}"'


def build_query(entity: str, query_term: str, bound: dict) -> str:
    """Assemble a Lucene query from the cell value plus any bound filters."""
    parts = []
    field = ENTITY_QUERY_FIELD[entity]
    if query_term:
        parts.append(f"{field}:{lucene_phrase(query_term)}")

    fmap = ENTITY_FILTER_FIELDS.get(entity, {})
    for pid in FILTER_PROPS:
        val = bound.get(pid)
        if not val or pid not in fmap:
            continue
        mb = fmap[pid]
        if pid == "year":
            yr = re.sub(r"[^0-9]", "", val)[:4]
            if yr:
                parts.append(f"{mb}:{yr}*")
        elif pid == "country":
            parts.append(f"{mb}:{val.strip()}")
        elif pid == "barcode":
            bc = re.sub(r"[^0-9]", "", val)
            if bc:
                parts.append(f"{mb}:{bc}")
        else:
            parts.append(f"{mb}:{lucene_phrase(val)}")

    return " AND ".join(parts) if parts else query_term


def candidate_artist(cand: dict) -> str:
    return " ".join(e.get("name", "") for e in cand.get("artist-credit") or []).strip()


def ms_to_mmss(ms) -> str:
    try:
        total = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    return f"{total // 60}:{total % 60:02d}"


def score_candidate(entity: str, query_term: str, bound: dict, cand: dict) -> float:
    name = cand.get("title") if entity in TITLE_ENTITIES else cand.get("name")
    score = fuzz.token_set_ratio(normalize(query_term), normalize(name or ""))

    if bound.get("artist") and entity in TITLE_ENTITIES:
        a = fuzz.token_set_ratio(normalize(bound["artist"]),
                                 normalize(candidate_artist(cand)))
        score = 0.6 * score + 0.4 * a

    # hard-evidence boosts from the candidate's structured fields
    if bound.get("catno"):
        cand_catnos = [li.get("catalog-number", "")
                       for li in cand.get("label-info") or []]
        if any(norm_catno(bound["catno"]) == norm_catno(c) for c in cand_catnos if c):
            score = min(100.0, score + 15)
    if bound.get("barcode") and cand.get("barcode"):
        if re.sub(r"\D", "", bound["barcode"]) == re.sub(r"\D", "", cand["barcode"]):
            score = min(100.0, score + 10)
    if bound.get("year") and str(cand.get("date", "")).startswith(bound["year"][:4]):
        score = min(100.0, score + 5)
    if bound.get("country") and cand.get("country"):
        if bound["country"].strip().lower() == str(cand["country"]).lower():
            score = min(100.0, score + 3)

    return score


def display_name(entity: str, cand: dict) -> str:
    bits = []
    if entity in TITLE_ENTITIES:
        title = cand.get("title", "")
        artist = candidate_artist(cand)
        name = f"{artist} – {title}" if artist else title
        if entity == "release":
            if cand.get("date"):
                bits.append(str(cand["date"])[:4])
            if cand.get("country"):
                bits.append(cand["country"])
            li = cand.get("label-info") or []
            if li:
                lab = (li[0].get("label") or {}).get("name", "")
                cat = li[0].get("catalog-number", "")
                lb = " ".join(x for x in [lab, cat] if x)
                if lb:
                    bits.append(lb)
        elif entity == "release-group":
            if cand.get("primary-type"):
                bits.append(cand["primary-type"])
            if cand.get("first-release-date"):
                bits.append(str(cand["first-release-date"])[:4])
        elif entity == "recording":
            if cand.get("length"):
                bits.append(ms_to_mmss(cand["length"]))
    else:
        name = cand.get("name", "")
        if cand.get("type"):
            bits.append(cand["type"])
        if entity == "artist":
            if cand.get("country"):
                bits.append(cand["country"])
            ls = cand.get("life-span") or {}
            if ls.get("begin"):
                bits.append(str(ls["begin"])[:4])
        elif entity == "label":
            area = (cand.get("area") or {}).get("name", "")
            if area:
                bits.append(area)

    extras = " · ".join(b for b in bits if b)
    if extras:
        name = f"{name} ({extras})"
    if cand.get("disambiguation"):
        name = f"{name} [{cand['disambiguation']}]"
    return name


# --- reconciliation core ----------------------------------------------------

def reconcile_one(q: dict) -> dict:
    query_term = q.get("query", "")
    limit = q.get("limit") or 5
    qtype = q.get("type") or "release"
    if isinstance(qtype, list):
        qtype = qtype[0] if qtype else "release"
    if isinstance(qtype, dict):
        qtype = qtype.get("id", "release")
    if qtype not in ENTITY_QUERY_FIELD:
        qtype = "release"

    bound = {pid: prop_value(q.get("properties"), pid) for pid in FILTER_PROPS}

    query = build_query(qtype, query_term, bound)
    data = mb_get(f"/{qtype}", {"query": query, "limit": 10})
    hits = (data or {}).get(RESULT_KEY[qtype], [])
    log.info("search %r [%s] (filters %s) -> %d hits",
             query_term, qtype,
             {k: v for k, v in bound.items() if v} or "none", len(hits))

    # If structured filters were too strict and returned nothing, retry with
    # just the cell value (and artist, the most reliable narrowing field).
    if not hits and any(bound.values()):
        loose_bound = {"artist": bound.get("artist", "")}
        loose_q = build_query(qtype, query_term, loose_bound)
        loose = mb_get(f"/{qtype}", {"query": loose_q, "limit": 10})
        hits = (loose or {}).get(RESULT_KEY[qtype], [])
        log.info("  loose retry -> %d hits", len(hits))

    results = []
    for cand in hits:
        cid = cand.get("id")
        if not cid:
            continue
        score = score_candidate(qtype, query_term, bound, cand)
        results.append({
            "id": f"{qtype}/{cid}",
            "name": display_name(qtype, cand),
            "type": [{"id": qtype, "name": ENTITY_LABELS[qtype]}],
            "score": round(score, 1),
            "match": score >= 95,
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    if results:
        log.info("  top: %.1f  %s", results[0]["score"], results[0]["name"])
    return {"result": results[:limit]}


# --- data extension -----------------------------------------------------------

EXTEND_PROPS_BY_TYPE = {
    "release": [
        {"id": "mbid", "name": "MBID"},
        {"id": "release_group_mbid", "name": "Release group MBID"},
        {"id": "artist", "name": "Artist"},
        {"id": "date", "name": "Date"},
        {"id": "country", "name": "Country"},
        {"id": "labels", "name": "Label(s)"},
        {"id": "catno", "name": "Catalog number"},
        {"id": "barcode", "name": "Barcode"},
        {"id": "formats", "name": "Format(s)"},
        {"id": "status", "name": "Status"},
        {"id": "packaging", "name": "Packaging"},
        {"id": "track_count", "name": "Track count"},
        {"id": "musicbrainz_url", "name": "MusicBrainz URL"},
        {"id": "tracklist_json", "name": "Tracklist (JSON: position/title/length)"},
    ],
    "release-group": [
        {"id": "mbid", "name": "MBID"},
        {"id": "artist", "name": "Artist"},
        {"id": "primary_type", "name": "Primary type"},
        {"id": "secondary_types", "name": "Secondary types"},
        {"id": "first_release_date", "name": "First release date"},
        {"id": "musicbrainz_url", "name": "MusicBrainz URL"},
    ],
    "artist": [
        {"id": "mbid", "name": "MBID"},
        {"id": "sort_name", "name": "Sort name"},
        {"id": "type", "name": "Type"},
        {"id": "gender", "name": "Gender"},
        {"id": "country", "name": "Country"},
        {"id": "area", "name": "Area"},
        {"id": "begin", "name": "Begin"},
        {"id": "end", "name": "End"},
        {"id": "disambiguation", "name": "Disambiguation"},
        {"id": "musicbrainz_url", "name": "MusicBrainz URL"},
    ],
    "label": [
        {"id": "mbid", "name": "MBID"},
        {"id": "type", "name": "Type"},
        {"id": "label_code", "name": "Label code"},
        {"id": "area", "name": "Area"},
        {"id": "country", "name": "Country"},
        {"id": "begin", "name": "Begin"},
        {"id": "end", "name": "End"},
        {"id": "disambiguation", "name": "Disambiguation"},
        {"id": "musicbrainz_url", "name": "MusicBrainz URL"},
    ],
    "recording": [
        {"id": "mbid", "name": "MBID"},
        {"id": "artist", "name": "Artist"},
        {"id": "length", "name": "Length"},
        {"id": "isrcs", "name": "ISRCs"},
        {"id": "first_release_date", "name": "First release date"},
        {"id": "musicbrainz_url", "name": "MusicBrainz URL"},
    ],
}

# id -> meta, merged across all types (shared ids like "artist" have the same
# display name everywhere, so a flat dict is fine for the extend response meta).
ALL_EXTEND_META = {p["id"]: p
                   for props in EXTEND_PROPS_BY_TYPE.values() for p in props}


def get_entity_cached(entity_id: str) -> dict | None:
    """entity_id like 'release/<mbid>' or 'artist/<mbid>'."""
    with _cache_lock:
        if entity_id in _entity_cache:
            return _entity_cache[entity_id]
    kind, _, mbid = entity_id.partition("/")
    if kind not in ENTITY_QUERY_FIELD:
        return None
    params = {}
    if LOOKUP_INC.get(kind):
        params["inc"] = LOOKUP_INC[kind]
    data = mb_get(f"/{kind}/{mbid}", params)
    if data is not None:
        with _cache_lock:
            _entity_cache[entity_id] = data
    return data


def field_value(kind: str, mbid: str, d: dict, pid: str):
    """Raw (string-or-empty) value for one extension property."""
    url = f"https://musicbrainz.org/{kind}/{mbid}"
    if pid == "mbid":
        return mbid
    if pid == "musicbrainz_url":
        return url
    if pid == "artist":
        return candidate_artist(d)
    if pid == "disambiguation":
        return d.get("disambiguation", "")

    if kind == "release":
        if pid == "release_group_mbid":
            return (d.get("release-group") or {}).get("id", "")
        if pid == "date":
            return d.get("date", "")
        if pid == "country":
            return d.get("country", "")
        if pid == "labels":
            return ", ".join((li.get("label") or {}).get("name", "")
                             for li in d.get("label-info") or []
                             if (li.get("label") or {}).get("name"))
        if pid == "catno":
            return ", ".join(li.get("catalog-number", "")
                             for li in d.get("label-info") or []
                             if li.get("catalog-number"))
        if pid == "barcode":
            return d.get("barcode", "")
        if pid == "formats":
            return ", ".join(m.get("format", "") for m in d.get("media") or []
                             if m.get("format"))
        if pid == "status":
            return d.get("status", "")
        if pid == "packaging":
            return d.get("packaging", "")
        if pid == "track_count":
            n = sum(m.get("track-count", 0) for m in d.get("media") or [])
            return str(n) if n else ""
        if pid == "tracklist_json":
            tl = [{"position": t.get("number") or t.get("position"),
                   "title": t.get("title"),
                   "length": ms_to_mmss(t.get("length"))}
                  for m in d.get("media") or [] for t in m.get("tracks") or []]
            return json.dumps(tl, ensure_ascii=False) if tl else ""

    if kind == "release-group":
        if pid == "primary_type":
            return d.get("primary-type", "")
        if pid == "secondary_types":
            return ", ".join(d.get("secondary-types") or [])
        if pid == "first_release_date":
            return d.get("first-release-date", "")

    if kind == "artist":
        if pid == "sort_name":
            return d.get("sort-name", "")
        if pid == "type":
            return d.get("type", "")
        if pid == "gender":
            return d.get("gender", "")
        if pid == "country":
            return d.get("country", "")
        if pid == "area":
            return (d.get("area") or {}).get("name", "")
        if pid == "begin":
            return (d.get("life-span") or {}).get("begin", "")
        if pid == "end":
            return (d.get("life-span") or {}).get("end", "")

    if kind == "label":
        if pid == "type":
            return d.get("type", "")
        if pid == "label_code":
            return str(d.get("label-code") or "")
        if pid == "area":
            return (d.get("area") or {}).get("name", "")
        if pid == "country":
            return d.get("country", "")
        if pid == "begin":
            return (d.get("life-span") or {}).get("begin", "")
        if pid == "end":
            return (d.get("life-span") or {}).get("end", "")

    if kind == "recording":
        if pid == "length":
            return ms_to_mmss(d.get("length"))
        if pid == "isrcs":
            return ", ".join(d.get("isrcs") or [])
        if pid == "first_release_date":
            return d.get("first-release-date", "")

    return ""


def extend_entity(entity_id: str, prop_ids: list[str]) -> dict:
    d = get_entity_cached(entity_id)
    if d is None:
        return {pid: [] for pid in prop_ids}
    kind, _, mbid = entity_id.partition("/")

    def s(v):
        return [{"str": str(v)}] if v not in (None, "", []) else []

    return {pid: s(field_value(kind, mbid, d, pid)) for pid in prop_ids}


# --- flask routes -----------------------------------------------------------

MANIFEST = {
    "versions": ["0.2"],
    "name": SERVICE_NAME,
    "identifierSpace": "https://musicbrainz.org/",
    "schemaSpace": "https://musicbrainz.org/",
    "defaultTypes": [{"id": k, "name": v} for k, v in ENTITY_LABELS.items()],
    "view": {"url": "https://musicbrainz.org/{{id}}"},
    "preview": {
        "url": f"http://localhost:{PORT}/reconcile/preview?id={{{{id}}}}",
        "width": 430,
        "height": 130,
    },
    "suggest": {
        "property": {
            "service_url": f"http://localhost:{PORT}",
            "service_path": "/reconcile/suggest/property",
        },
    },
    "extend": {
        "propose_properties": {
            "service_url": f"http://localhost:{PORT}",
            "service_path": "/reconcile/propose_properties",
        },
        "property_settings": [],
    },
}


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def jsonp_or_json(payload):
    callback = request.values.get("callback")
    if callback:
        body = f"{callback}({json.dumps(payload)});"
        return app.response_class(body, mimetype="application/javascript")
    return jsonify(payload)


@app.route("/reconcile", methods=["GET", "POST", "OPTIONS"])
def reconcile():
    if request.method == "OPTIONS":
        return ("", 204)
    queries = request.values.get("queries")
    extend = request.values.get("extend")

    if queries:
        qs = json.loads(queries)
        return jsonp_or_json({key: reconcile_one(q) for key, q in qs.items()})

    if extend:
        payload = json.loads(extend)
        prop_ids = [p["id"] for p in payload.get("properties", [])]
        rows = {eid: extend_entity(eid, prop_ids) for eid in payload.get("ids", [])}
        return jsonp_or_json({
            "meta": [ALL_EXTEND_META[pid] for pid in prop_ids if pid in ALL_EXTEND_META],
            "rows": rows,
        })

    return jsonp_or_json(MANIFEST)


@app.route("/reconcile/suggest/property")
def suggest_property():
    prefix = (request.args.get("prefix") or "").lower()
    matches = [p for p in FILTER_PROP_META
               if prefix in p["id"] or prefix in p["name"].lower()]
    return jsonp_or_json({
        "code": "/api/status/ok",
        "status": "200 OK",
        "prefix": prefix,
        "result": matches or FILTER_PROP_META,
    })


@app.route("/reconcile/propose_properties")
def propose_properties():
    qtype = request.args.get("type", "release")
    props = EXTEND_PROPS_BY_TYPE.get(qtype, EXTEND_PROPS_BY_TYPE["release"])
    return jsonp_or_json({"type": qtype, "properties": props})


@app.route("/reconcile/preview")
def preview():
    entity_id = request.args.get("id", "")
    kind = entity_id.partition("/")[0]
    d = get_entity_cached(entity_id)
    if d is None:
        return "<html><body>Not found</body></html>"

    if kind in TITLE_ENTITIES:
        title = d.get("title", "")
        artist = candidate_artist(d)
        lines = [f"<b>{title}</b>"]
        if artist:
            lines.append(artist)
        if kind == "release":
            li = ", ".join(
                f"{(x.get('label') or {}).get('name','')} {x.get('catalog-number','')}".strip()
                for x in d.get("label-info") or [])
            n = sum(m.get("track-count", 0) for m in d.get("media") or [])
            lines.append(f"{li}")
            lines.append(f"{d.get('country','')} {d.get('date','')} · {n} tracks")
            mbid = entity_id.partition("/")[2]
            thumb = f"https://coverartarchive.org/release/{mbid}/front-250"
        elif kind == "release-group":
            lines.append(f"{d.get('primary-type','')} · {d.get('first-release-date','')}")
            thumb = ""
        else:  # recording
            lines.append(f"{ms_to_mmss(d.get('length'))}")
            thumb = ""
    else:
        name = d.get("name", "")
        lines = [f"<b>{name}</b>", d.get("type", "")]
        if kind == "artist":
            ls = d.get("life-span") or {}
            lines.append(" – ".join(x for x in [ls.get("begin", ""), ls.get("end", "")] if x))
            lines.append((d.get("area") or {}).get("name", "") or d.get("country", ""))
        elif kind == "label":
            lines.append((d.get("area") or {}).get("name", "") or d.get("country", ""))
        thumb = ""

    if d.get("disambiguation"):
        lines.append(f"<i>{d['disambiguation']}</i>")
    body = "<br/>".join(x for x in lines if x)
    img = (f"<img src='{thumb}' width='100' height='100' "
           f"style='object-fit:cover'/>") if thumb else ""
    return (
        "<html><body style='font-family:sans-serif;font-size:13px;display:flex;gap:10px'>"
        f"{img}<div>{body}</div></body></html>"
    )


if __name__ == "__main__":
    log.info("MusicBrainz needs no API key; identifying as: %s", USER_AGENT)
    log.info("Throttling to ~1 request/second per MusicBrainz policy.")
    print(f"OpenRefine reconciliation endpoint: http://localhost:{PORT}/reconcile")
    app.run(host=HOST, port=PORT, threaded=True)
