import csv
import os
import re
from pathlib import Path
from statistics import mean
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient, UpdateOne

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CSV_PATH = BASE_DIR / "20260320_Neorx-treeline-planning.csv"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "farmplan")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "treeline_planning")
TREELINE_CSV_PATH = Path(os.getenv("TREELINE_CSV_PATH", str(DEFAULT_CSV_PATH))).resolve()
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})


def normalize_key(key: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", key.strip().lower())
    return cleaned.strip("_")


def parse_range_midpoint(value: str):
    if not value:
        return None

    normalized = value.replace("–", "-")
    matches = re.findall(r"\d+(?:[\.,]\d+)?", normalized)

    if not matches:
        return None

    values = [float(number.replace(",", ".")) for number in matches]
    if len(values) == 1:
        return values[0]

    return round((values[0] + values[1]) / 2, 2)


def split_zones(value: str):
    if not value:
        return []

    return [zone.strip() for zone in value.split(";") if zone.strip()]


def parse_int(value, default, min_value=None, max_value=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default

    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)

    return parsed


def to_document(row: dict):
    doc = {}

    for key, value in row.items():
        if not isinstance(key, str):
            continue

        normalized = normalize_key(key)
        if not normalized:
            continue

        doc[normalized] = value.strip() if isinstance(value, str) else value

    doc["source_id"] = doc.get("id")
    doc["end_height_mid_m"] = parse_range_midpoint(doc.get("end_height_m"))
    doc["lifespan_mid_years"] = parse_range_midpoint(doc.get("lifespan_years"))
    doc["expected_calories_mid"] = parse_range_midpoint(
        doc.get("expected_annual_calories_kcal_plant_mature")
    )
    doc["typical_share_mid_pct"] = parse_range_midpoint(doc.get("typical_share"))
    doc["hardiness_zone_list"] = split_zones(doc.get("hardiness_zones"))
    doc["hardiness_zone_count"] = len(doc["hardiness_zone_list"])

    return doc


def get_collection():
    client = MongoClient(MONGO_URI)
    return client, client[MONGO_DB][MONGO_COLLECTION]


def import_csv_to_mongo(collection, csv_path: Path, reset: bool = True):
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    if reset:
        collection.delete_many({})

    operations = []
    imported_rows = 0

    with csv_path.open(mode="r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)

        for row in reader:
            doc = to_document(row)
            if not doc.get("source_id"):
                continue

            operations.append(
                UpdateOne(
                    {"source_id": doc["source_id"]},
                    {"$set": doc},
                    upsert=True,
                )
            )
            imported_rows += 1

            if len(operations) >= 500:
                collection.bulk_write(operations, ordered=False)
                operations = []

    if operations:
        collection.bulk_write(operations, ordered=False)

    return imported_rows


def build_overview_payload(collection):
    rows = list(
        collection.find(
            {},
            {
                "_id": 0,
                "source_id": 1,
                "german_name": 1,
                "english_name": 1,
                "strata": 1,
                "category": 1,
                "hardiness_zones": 1,
                "typical_share": 1,
                "expected_calories_mid": 1,
                "end_height_mid_m": 1,
            },
        )
    )

    total_entries = len(rows)
    categories = sorted({row.get("category") for row in rows if row.get("category")})
    hardiness_zone_total = len(
        {zone for row in rows for zone in split_zones(row.get("hardiness_zones", ""))}
    )

    heights = [row["end_height_mid_m"] for row in rows if row.get("end_height_mid_m") is not None]
    calories = [row["expected_calories_mid"] for row in rows if row.get("expected_calories_mid") is not None]

    avg_height = round(mean(heights), 2) if heights else 0
    avg_calories = round(mean(calories), 0) if calories else 0

    stats = [
        {"label": "Total entries", "value": f"{total_entries}", "unit": "plants"},
        {"label": "Categories", "value": f"{len(categories)}", "unit": "types"},
        {"label": "Avg end height", "value": f"{avg_height}", "unit": "m"},
        {"label": "Mean calories", "value": f"{int(avg_calories)}", "unit": "kcal"},
    ]

    field_logs = []
    for row in rows[:14]:
        field_logs.append(
            {
                "id": row.get("source_id"),
                "name": row.get("german_name") or row.get("english_name") or "Unknown",
                "crop": row.get("english_name") or "Unknown",
                "strata": row.get("strata") or "n/a",
                "typicalShare": row.get("typical_share") or "n/a",
                "hardiness": row.get("hardiness_zones") or "n/a",
            }
        )

    performance = []
    highest_calorie_rows = sorted(
        [row for row in rows if row.get("expected_calories_mid")],
        key=lambda row: row["expected_calories_mid"],
        reverse=True,
    )[:10]

    for row in highest_calorie_rows:
        performance.append(
            {
                "week": row.get("source_id"),
                "value": round(row.get("expected_calories_mid", 0)),
            }
        )

    return {
        "stats": stats,
        "fieldLogs": field_logs,
        "performance": performance,
        "meta": {
            "csvPath": str(TREELINE_CSV_PATH),
            "totalEntries": total_entries,
            "hardinessZones": hardiness_zone_total,
        },
    }


@app.get("/api/health")
def health_check():
    return jsonify({"status": "ok"})


@app.post("/api/treeline/import")
def import_treeline_csv():
    payload = request.get_json(silent=True) or {}
    reset = payload.get("reset", True)
    csv_path = Path(payload.get("csvPath", TREELINE_CSV_PATH)).resolve()

    client, collection = get_collection()
    try:
        imported_rows = import_csv_to_mongo(collection, csv_path=csv_path, reset=reset)
        return jsonify(
            {
                "status": "ok",
                "importedRows": imported_rows,
                "csvPath": str(csv_path),
            }
        )
    except Exception as error:  # pragma: no cover - defensive endpoint guard
        return jsonify({"status": "error", "error": str(error)}), 500
    finally:
        client.close()


@app.get("/api/treeline/overview")
def get_treeline_overview():
    client, collection = get_collection()
    try:
        if collection.count_documents({}) == 0 and TREELINE_CSV_PATH.exists():
            import_csv_to_mongo(collection, csv_path=TREELINE_CSV_PATH, reset=True)

        payload = build_overview_payload(collection)
        return jsonify(payload)
    except Exception as error:  # pragma: no cover - defensive endpoint guard
        return jsonify({"status": "error", "error": str(error)}), 500
    finally:
        client.close()


@app.get("/api/treeline/records")
def get_treeline_records():
    page = parse_int(request.args.get("page", 1), default=1, min_value=1)
    limit = parse_int(request.args.get("limit", 10), default=10, min_value=1, max_value=100)
    search = (request.args.get("search") or "").strip()
    category = (request.args.get("category") or "all").strip()
    strata = (request.args.get("strata") or "all").strip()

    query = {}
    if category and category.lower() != "all":
        query["category"] = category
    if strata and strata.lower() != "all":
        query["strata"] = strata
    if search:
        query["$or"] = [
            {"source_id": {"$regex": search, "$options": "i"}},
            {"german_name": {"$regex": search, "$options": "i"}},
            {"english_name": {"$regex": search, "$options": "i"}},
            {"latin_name": {"$regex": search, "$options": "i"}},
        ]

    client, collection = get_collection()
    try:
        if collection.count_documents({}) == 0 and TREELINE_CSV_PATH.exists():
            import_csv_to_mongo(collection, csv_path=TREELINE_CSV_PATH, reset=True)

        total = collection.count_documents(query)
        total_pages = max(1, (total + limit - 1) // limit)
        page = min(page, total_pages)
        skip = (page - 1) * limit

        # CHANGED: Replaced the strict projection with a simple {"_id": 0}
        # This ensures ALL CSV data (including sources and purposes) is sent to the frontend
        raw_rows = list(
            collection.find(query, {"_id": 0})
            .sort("source_id", 1)
            .skip(skip)
            .limit(limit)
        )

        # CHANGED: Added 'calories' and 'rawDetails' to the response mapped to the frontend
        rows = [
            {
                "id": row.get("source_id"),
                "name": row.get("german_name") or row.get("english_name") or "Unknown",
                "crop": row.get("english_name") or "Unknown",
                "strata": row.get("strata") or "n/a",
                "typicalShare": row.get("typical_share") or "n/a",
                "hardiness": row.get("hardiness_zones") or "n/a",
                "category": row.get("category") or "n/a",
                "calories": row.get("expected_calories_mid") or 0,
                "rawDetails": row  # Passes the entire dictionary to the React app
            }
            for row in raw_rows
        ]

        categories = sorted(
            [item for item in collection.distinct("category") if item]
        )
        strata_options = sorted([item for item in collection.distinct("strata") if item])

        return jsonify(
            {
                "records": rows,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "totalPages": total_pages,
                    "hasPrev": page > 1,
                    "hasNext": page < total_pages,
                },
                "filters": {
                    "search": search,
                    "category": category,
                    "strata": strata,
                },
                "options": {
                    "categories": categories,
                    "strata": strata_options,
                },
            }
        )
    except Exception as error:  # pragma: no cover - defensive endpoint guard
        return jsonify({"status": "error", "error": str(error)}), 500
    finally:
        client.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True)