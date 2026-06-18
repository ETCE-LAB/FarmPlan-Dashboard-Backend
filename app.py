import os
import re
import urllib.parse
from datetime import date
from pathlib import Path
from statistics import mean

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient, UpdateOne

load_dotenv()

# ── Mongo config ──────────────────────────────────────────────────────────────
MONGO_USERNAME   = os.getenv("MONGO_USERNAME")
MONGO_PASSWORD   = os.getenv("MONGO_PASSWORD")
MONGO_CLUSTER    = os.getenv("MONGO_CLUSTER", "localhost:27017")
MONGO_DB         = os.getenv("MONGO_DB", "farmplan")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "treeline_planning")

if MONGO_USERNAME and MONGO_PASSWORD:
    encoded_pass = urllib.parse.quote_plus(MONGO_PASSWORD)
    MONGO_URI = (
        f"mongodb+srv://{MONGO_USERNAME}:{encoded_pass}@{MONGO_CLUSTER}"
        f"/?retryWrites=true&w=majority"
    )
else:
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# ── Hardiness raster (optional — only needed for /api/hardiness/field) ────────
BASE_DIR               = Path(__file__).resolve().parent
DEFAULT_HARDINESS_PATH = BASE_DIR / "hardiness_zones_1990_2024_every_2y.tif"
HARDINESS_RASTER_PATH  = Path(os.getenv("HARDINESS_RASTER_PATH", str(DEFAULT_HARDINESS_PATH))).resolve()

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Raster imports (lazy — only fail at runtime if raster is actually called) ─
import numpy as np
import rasterio
from rasterio.mask import mask as rasterio_mask
from shapely.geometry import Polygon
from shapely.validation import make_valid

INT_TO_ZONE = {
    0: "<6a", 1: "6a",  2: "6b",  3: "7a",  4: "7b",
    5: "8a",  6: "8b",  7: "9a",  8: "9b",  9: ">9b",
}
ZONE_TO_TMP = {
    "<6a": (-53.9, -23.3), "6a":  (-23.3, -20.6), "6b":  (-20.6, -17.8),
    "7a":  (-17.8, -15.0), "7b":  (-15.0, -12.2), "8a":  (-12.2,  -9.4),
    "8b":  ( -9.4,  -6.7), "9a":  ( -6.7,  -3.9), "9b":  ( -3.9,  -1.1),
    ">9b": ( -1.1,  21.1),
}

TODAY = str(date.today())


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def get_collection():
    client = MongoClient(MONGO_URI)
    return client, client[MONGO_DB][MONGO_COLLECTION]


def split_zones(value: str) -> list:
    if not value:
        return []
    return [z.strip() for z in value.split(";") if z.strip()]


def parse_int(value, default, min_value=None, max_value=None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def get_hardiness_polygon(polygon_coords: list) -> dict:
    if not polygon_coords or len(polygon_coords) < 3:
        raise ValueError("Polygon must contain at least 3 coordinates")
    polygon_lonlat = [(lng, lat) for lat, lng in polygon_coords]
    polygon = make_valid(Polygon(polygon_lonlat))
    with rasterio.open(HARDINESS_RASTER_PATH) as src:
        out_image, _ = rasterio_mask(src, [polygon], crop=True, all_touched=True)
        data  = out_image[0]
        valid = data[data != src.nodata] if src.nodata is not None else data[data >= 0]
        if len(valid) == 0:
            return {"dominantZone": None, "distribution": {}, "rawPixelCount": None, "temperature": None}
        raw_pixel_count = len(valid)
        values, counts  = np.unique(valid, return_counts=True)
        total = counts.sum()
        distribution: dict = {}
        for value, count in zip(values, counts):
            zone = INT_TO_ZONE.get(int(value), f"unknown({int(value)})")
            distribution[zone] = round((count / total) * 100, 2)
        dominant_zone = max(distribution, key=distribution.get)
        return {
            "dominantZone": dominant_zone,
            "distribution": distribution,
            "rawPixelCount": raw_pixel_count,
            "temperature":   ZONE_TO_TMP.get(dominant_zone),
        }


def build_overview_payload(collection) -> dict:
    rows = list(collection.find({}, {
        "_id": 0,
        "source_id": 1, "german_name": 1, "english_name": 1,
        "strata": 1, "category": 1, "hardiness_zones": 1,
        "typical_share": 1, "expected_calories_mid": 1, "end_height_mid_m": 1,
    }))

    total_entries        = len(rows)
    categories           = sorted({r.get("category") for r in rows if r.get("category")})
    hardiness_zone_total = len({z for r in rows for z in split_zones(r.get("hardiness_zones", ""))})
    heights  = [r["end_height_mid_m"]      for r in rows if r.get("end_height_mid_m")      is not None]
    calories = [r["expected_calories_mid"]  for r in rows if r.get("expected_calories_mid") is not None]

    stats = [
        {"label": "Total entries",  "value": str(total_entries),                                     "unit": "plants"},
        {"label": "Categories",     "value": str(len(categories)),                                   "unit": "types"},
        {"label": "Avg end height", "value": str(round(mean(heights), 2) if heights else 0),         "unit": "m"},
        {"label": "Mean calories",  "value": str(int(round(mean(calories), 0)) if calories else 0),  "unit": "kcal"},
    ]

    field_logs = [
        {
            "id":           r.get("source_id"),
            "name":         r.get("german_name") or r.get("english_name") or "Unknown",
            "crop":         r.get("english_name") or "Unknown",
            "strata":       r.get("strata")         or "n/a",
            "typicalShare": r.get("typical_share")  or "n/a",
            "hardiness":    r.get("hardiness_zones") or "n/a",
        }
        for r in rows[:14]
    ]

    performance = [
        {"week": r.get("source_id"), "value": round(r.get("expected_calories_mid", 0))}
        for r in sorted(
            [r for r in rows if r.get("expected_calories_mid")],
            key=lambda r: r["expected_calories_mid"],
            reverse=True,
        )[:10]
    ]

    return {
        "stats":       stats,
        "fieldLogs":   field_logs,
        "performance": performance,
        "meta": {
            "totalEntries":   total_entries,
            "hardinessZones": hardiness_zone_total,
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES — HEALTH
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health_check():
    client, collection = get_collection()
    try:
        count = collection.count_documents({})
        return jsonify({"status": "ok", "documents": count})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        client.close()


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES — TREELINE (reads from DB only, no CSV)
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/treeline/overview")
def get_treeline_overview():
    client, collection = get_collection()
    try:
        count = collection.count_documents({})
        if count == 0:
            return jsonify({"status": "error", "error": "Collection is empty. No CSV fallback — please seed the database."}), 404
        return jsonify(build_overview_payload(collection))
    except Exception as error:
        return jsonify({"status": "error", "error": str(error)}), 500
    finally:
        client.close()


@app.get("/api/treeline/records")
def get_treeline_records():
    page      = parse_int(request.args.get("page",  1),   default=1,  min_value=1)
    limit     = parse_int(request.args.get("limit", 10),  default=10, min_value=1, max_value=100)
    search    = (request.args.get("search")    or "").strip()
    category  = (request.args.get("category")  or "all").strip()
    strata    = (request.args.get("strata")    or "all").strip()
    hardiness = (request.args.get("hardiness") or "all").strip()

    query: dict = {}
    if category  and category.lower()  != "all": query["category"]           = category
    if strata    and strata.lower()    != "all": query["strata"]              = strata
    if hardiness and hardiness.lower() != "all": query["hardiness_zone_list"] = hardiness
    if search:
        query["$or"] = [
            {"source_id":    {"$regex": search, "$options": "i"}},
            {"german_name":  {"$regex": search, "$options": "i"}},
            {"english_name": {"$regex": search, "$options": "i"}},
            {"latin_name":   {"$regex": search, "$options": "i"}},
        ]

    client, collection = get_collection()
    try:
        total       = collection.count_documents(query)
        total_pages = max(1, (total + limit - 1) // limit)
        page        = min(page, total_pages)
        skip        = (page - 1) * limit

        raw_rows = list(
            collection.find(query, {"_id": 0})
            .sort("source_id", 1)
            .skip(skip)
            .limit(limit)
        )

        rows = [
            {
                "id":           row.get("source_id"),
                "name":         row.get("german_name") or row.get("english_name") or "Unknown",
                "crop":         row.get("english_name") or "Unknown",
                "strata":       row.get("strata")         or "n/a",
                "typicalShare": row.get("typical_share")  or "n/a",
                "hardiness":    row.get("hardiness_zones") or "n/a",
                "category":     row.get("category")       or "n/a",
                "calories":     row.get("expected_calories_mid") or 0,
                "image":        row.get("image"),          # None for plants without image
                "rawDetails":   row,
            }
            for row in raw_rows
        ]

        return jsonify({
            "records": rows,
            "pagination": {
                "page": page, "limit": limit, "total": total,
                "totalPages": total_pages,
                "hasPrev": page > 1, "hasNext": page < total_pages,
            },
            "filters":  {"search": search, "category": category, "strata": strata, "hardiness": hardiness},
            "options": {
                "categories":     sorted([x for x in collection.distinct("category")            if x]),
                "strata":         sorted([x for x in collection.distinct("strata")              if x]),
                "hardinessZones": sorted([x for x in collection.distinct("hardiness_zone_list") if x]),
            },
        })
    except Exception as error:
        return jsonify({"status": "error", "error": str(error)}), 500
    finally:
        client.close()


@app.get("/api/treeline/records/<source_id>")
def get_treeline_record(source_id: str):
    """Return full details for a single plant by source_id."""
    client, collection = get_collection()
    try:
        doc = collection.find_one({"source_id": source_id.upper()}, {"_id": 0})
        if not doc:
            return jsonify({"status": "not_found", "source_id": source_id}), 404
        return jsonify({"status": "ok", "record": doc})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        client.close()


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES — PLANT IMAGES (sub-document on treeline_planning)
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/plant-images")
def list_plant_images():
    """Return all plants that have an embedded image sub-document."""
    client, collection = get_collection()
    try:
        docs = list(collection.find(
            {"image": {"$exists": True}},
            {"_id": 0, "source_id": 1, "image": 1},
        ))
        images = [{"source_id": d["source_id"], **d["image"]} for d in docs]
        return jsonify({"status": "ok", "count": len(images), "images": images})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        client.close()


@app.get("/api/plant-images/<source_id>")
def get_plant_image(source_id: str):
    """Return the embedded image for one plant."""
    client, collection = get_collection()
    try:
        doc = collection.find_one(
            {"source_id": source_id.upper()},
            {"_id": 0, "source_id": 1, "image": 1},
        )
        if not doc or not doc.get("image"):
            return jsonify({"status": "not_found", "source_id": source_id}), 404
        return jsonify({"status": "ok", "image": {"source_id": doc["source_id"], **doc["image"]}})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        client.close()


@app.put("/api/plant-images/<source_id>")
def upsert_plant_image(source_id: str):
    """Add or update the embedded image for a plant."""
    REQUIRED = ["image_url", "source_page_url", "license_name", "license_url", "author_name"]
    payload  = request.get_json(silent=True) or {}
    missing  = [f for f in REQUIRED if not payload.get(f)]
    if missing:
        return jsonify({"status": "error", "error": f"Missing required fields: {missing}"}), 400

    image_doc = {
        "image_url":       payload["image_url"],
        "source_page_url": payload["source_page_url"],
        "license_name":    payload["license_name"],
        "license_url":     payload["license_url"],
        "author_name":     payload["author_name"],
        "author_url":      payload.get("author_url", ""),
        "plant_name":      payload.get("plant_name", ""),
        "date_accessed":   payload.get("date_accessed", TODAY),
    }

    client, collection = get_collection()
    try:
        result = collection.update_one(
            {"source_id": source_id.upper()},
            {"$set": {"image": image_doc}},
        )
        if result.matched_count == 0:
            return jsonify({"status": "not_found", "source_id": source_id}), 404
        return jsonify({"status": "ok", "source_id": source_id.upper(), "image": image_doc})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        client.close()


@app.delete("/api/plant-images/<source_id>")
def delete_plant_image(source_id: str):
    """Remove the embedded image from a plant document."""
    client, collection = get_collection()
    try:
        result = collection.update_one(
            {"source_id": source_id.upper()},
            {"$unset": {"image": ""}},
        )
        if result.matched_count == 0:
            return jsonify({"status": "not_found", "source_id": source_id}), 404
        return jsonify({"status": "ok", "deleted_image_for": source_id.upper()})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        client.close()


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES — HARDINESS
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/api/hardiness/field")
def get_field_hardiness():
    payload = request.get_json(silent=True) or {}
    polygon = payload.get("polygon")
    if not polygon or not isinstance(polygon, list):
        return jsonify({"status": "error", "error": "polygon is required and must be a list"}), 400
    if len(polygon) < 3:
        return jsonify({"status": "error", "error": "polygon must have at least 3 points"}), 400
    try:
        result = get_hardiness_polygon(polygon)
        return jsonify({"status": "ok", **result})
    except Exception as error:
        return jsonify({"status": "error", "error": str(error)}), 500


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True)