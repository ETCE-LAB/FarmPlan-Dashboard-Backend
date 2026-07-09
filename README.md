# FarmPlan Backend

This folder contains the Flask backend for the FarmPlan dashboard. It reads the treeline CSV file, stores the data in MongoDB, and provides the API that the frontend uses.

## What it does

- checks if the backend is running with `/api/health`
- imports the CSV into MongoDB with `/api/treeline/import`
- returns dashboard summary data with `/api/treeline/overview`
- returns paginated plant records with `/api/treeline/records`
- automatically imports the CSV if the database is still empty

## What you need

- Python 3.10 or newer
- MongoDB running locally or through `MONGO_URI`
- the CSV file `20260320_Neorx-treeline-planning.csv` in this folder

## Setup

Open the `backend` folder and run:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

The backend uses these environment variables:

- `MONGO_URI` - MongoDB connection string. Default is `mongodb://localhost:27017`
- `MONGO_DB` - Database name. Default is `farmplan`
- `MONGO_COLLECTION` - Collection name. Default is `treeline_planning`
- `TREELINE_CSV_PATH` - Path to the CSV file used for imports. Default is `20260320_Neorx-treeline-planning.csv`
- `FLASK_PORT` - Port for the Flask server. Default is `5000`

If you want different values, create a `.env` file in this folder.

Example:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=farmplan
MONGO_COLLECTION=treeline_planning
TREELINE_CSV_PATH=20260320_Neorx-treeline-planning.csv
FLASK_PORT=5000
```

## Run it

Start the backend with:

```bash
python app.py
```

The server listens on `0.0.0.0` and uses the port from the config.

# Production Deployment (Docker & CI/CD)

The application is fully configured for production deployment using Docker and runtime configuration.


## 1. Backend Deployment (Docker)

The backend is containerized via Docker and requires system dependencies (like `libexpat1` for `rasterio`) which are pre-configured in the Dockerfile.

Ensure your server `.env` file is populated with production values:

```env
MONGO_URI=mongodb+srv://<user>:<password>@cluster.mongodb.net
FLASK_PORT=5000
FLASK_ENV=production
```

Build the Docker image:

```bash
docker build -t farmplan-dashboard-backend:latest .
```

Run the container:

```bash
docker run -p 5000:5000 --env-file .env farmplan-dashboard-backend:latest
```

## 2. Frontend Deployment (Runtime Configuration)

The frontend uses a dynamic runtime configuration pattern. This means the server administrator can change the backend API URL without needing to rebuild the React application.

Build the production assets:

```bash
npm run build
```

On your live server, locate the `dist/config.js` file.

Edit the file to point to your live Python backend:

```javascript
window.FARM_PLAN_CONFIG = {
  API_BASE_URL: "https://your-production-api-url.com"
};
```

The frontend will instantly begin routing traffic to the new URL.

## API Endpoints

### `GET /api/health`

Returns a simple status response to show the backend is working.

### `POST /api/treeline/import`

Imports the CSV into MongoDB.

Request body example:

```json
{
  "reset": true,
  "csvPath": "20260320_Neorx-treeline-planning.csv"
}
```

### `GET /api/treeline/overview`

Returns the summary data used by the dashboard, like stats, field logs, performance data, and metadata.

### `GET /api/treeline/records`

Returns the plant records in pages.

Query parameters:

- `page` - page number, default `1`
- `limit` - page size, default `10`, max `100`
- `search` - text search across source ID and plant names
- `category` - filter by category
- `strata` - filter by strata
- `hardiness` - filter by hardiness zone list

Example:

```bash
curl "http://localhost:5000/api/treeline/records?page=1&limit=10&search=oak"
```

## Data notes

- CSV headers are changed to lowercase snake_case before they go into MongoDB.
- Derived fields include `source_id`, `end_height_mid_m`, `lifespan_mid_years`, `expected_calories_mid`, `typical_share_mid_pct`, `hardiness_zone_list`, and `hardiness_zone_count`.
- If the collection is empty, the backend imports the default CSV the first time overview or records are requested.

## Frontend use

The frontend expects this backend to be available on `localhost:5000` during development.
