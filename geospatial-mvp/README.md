# Geospatial MVP — Single-Image Processing Pipeline

A runnable FastAPI + React pipeline that takes one aerial image, validates it, extracts
EXIF/GPS/GeoTIFF metadata, and produces whatever geospatial products the image can honestly
support — including a GeoTIFF and a Cloud Optimized GeoTIFF when georeferencing genuinely
exists — plus a detailed QC report.

The guiding rule: **it never invents coordinates, CRS, resolution or accuracy.** If an image
cannot be georeferenced, the pipeline says exactly what is missing instead of pretending.

---

## 1. What one image can and cannot produce

| Product | When it is generated | Notes |
|---|---|---|
| Original image copy | Always | Byte-identical copy of the upload |
| `metadata.json` | Always | Full EXIF, XMP presence, GeoTIFF tags |
| `preview.jpg` | Always | EXIF-rotation-corrected, max 1024 px |
| `processing_report.json` | Always | QC checks, product matrix, allocation method |
| Georeferenced GeoTIFF | Only with real georeferencing | see the three sources below |
| Cloud Optimized GeoTIFF | Only from a valid georeferenced GeoTIFF | tiled 512×512, overviews, monotonic tile offsets — verified by reading the TIFF IFD chain |
| DSM / DTM / point cloud / 3D model / true orthophoto | **Never** | These need many overlapping images and a photogrammetry engine (ODM, Metashape, Pix4D). This MVP deliberately does not fake them. |

Georeferencing is accepted from, in order of trust:

1. **`embedded_geotransform`** — the file already carries a CRS and affine transform (a real GeoTIFF).
2. **`caller_override`** — the caller supplies `crs` + origin + pixel size (the UI lets you draw a box on a map, which is projected to the local UTM zone).
3. **`gps_centre_footprint`** — EXIF GPS fix + altitude + focal length. GSD is computed as
   `altitude × sensor_width / (focal_length × image_width)`. This centres the raster on the fix and
   reports the resulting footprint. **It is an estimate**, flagged as `WARNING` in QC with
   `estimated_gsd: true`, and the report states plainly that it is not an orthophoto.

---

## 2. Quick start (local)

Requires Python 3.12+ (3.14 verified). `rasterio` and `pyproj` ship GDAL 3.12 / PROJ 9.8 inside their
wheels, so **no system GDAL installation is needed**.

```bash
cd geospatial-mvp

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env          # optional; defaults work out of the box

# Generate three deterministic sample images (EXIF-written, known ground truth)
python scripts/make_samples.py

# Run the API
uvicorn app.main:app --reload --port 8000
```

- API docs (Swagger): <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

Frontend (second terminal):

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /images /jobs /health to :8000
```

Run the tests:

```bash
pytest -q          # 46 tests
```

### Docker

```bash
docker compose up --build
# API  -> http://localhost:8000
# UI   -> http://localhost:8080   (nginx serves the SPA and proxies the API)
```

---

## 3. Architecture

Processing logic is completely separate from HTTP routes: routes only translate between
HTTP and the service layer, so the pipeline can be driven from tests or a background worker later.

```
app/
├── api/
│   ├── routes.py         HTTP endpoints (thin adapters only)
│   ├── deps.py           cached service singletons
│   └── middleware.py     request-size guard on upload paths
├── core/
│   ├── config.py         pydantic-settings (GEO_* env vars)
│   ├── errors.py         AppError -> structured JSON responses
│   └── logging.py        logging setup
├── models/
│   ├── domain.py         domain models: JobRecord, ProcessingReport, CheckResult, ...
│   └── api.py            request/response schemas
├── services/
│   ├── ingestion.py      upload -> validate -> extract -> persist
│   ├── metadata/
│   │   └── extractor.py  EXIF/GPS/XMP/GeoTIFF extraction
│   ├── processing/
│   │   ├── georef.py     decides HOW (and whether) the image can be placed
│   │   ├── raster.py     GeoTIFF writer, COG writer, COG validator, preview
│   │   └── pipeline.py   job orchestration + step timing
│   └── quality/
│       ├── validator.py  file/content/dimension validation
│       └── report.py     QC checks grouped by what they prove
└── storage/local.py      atomic local filesystem storage + download resolution
frontend/
├── src/api/client.js     typed-ish API client (structured ApiError)
├── src/api/utm.js        WGS84 -> UTM for map-drawn placements
└── src/components/       upload, metadata, map placement, process, outputs, QC report
```

**Jobs.** `POST /images/{id}/process` runs synchronously today, but every run is a persisted
`JobRecord` with per-step timing (`copy_original`, `load_metadata`, `georeference`, `write_geotiff`,
`write_cog`, `render_preview`, `quality_control`, `write_report`). Moving to a background queue
later means changing where `pipeline.process()` is called, not what it does.

---

## 4. API

All errors share one shape:

```json
{ "error": { "code": "invalid_image", "message": "…", "details": { "reason": "…" } } }
```

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/images/upload` | Upload + validate. `201` on success |
| `POST` | `/images/{image_id}/process` | Run the pipeline. Optional JSON body for an explicit placement |
| `GET` | `/images/{image_id}` | Image record, latest job, missing georeferencing inputs |
| `GET` | `/images/{image_id}/source` | Original image bytes |
| `GET` | `/images/{image_id}/metadata` | Full extracted metadata JSON |
| `GET` | `/jobs/{job_id}` | Job status, step timings, QC summary |
| `GET` | `/jobs/{job_id}/outputs` | Outputs + download URLs |
| `GET` | `/images/{i}/jobs/{j}/outputs/{file}` | Download an output |
| `GET` | `/images/{i}/jobs/{j}/report` | Processing report (JSON) |
| `GET` | `/images/{i}/jobs/{j}/report/download` | Report as a file |
| `GET` | `/health` | Status + GDAL/PROJ/storage checks |

Status codes: `201` created, `200` ok, `404` not found, `409` job already running, `413` too large,
`415` unsupported type / content mismatch, `422` corrupted image or invalid override, `500` processing failure.

### Example: end-to-end with curl

```bash
# 1. Upload
curl -s -X POST http://127.0.0.1:8000/images/upload \
     -F "file=@data/samples/aerial_gps.jpg;type=image/jpeg"
```

```json
{
  "image_id": "img_8a58ddcd208bc458a8446920",
  "original_filename": "aerial_gps.jpg",
  "size_bytes": 868864,
  "content_type": "image/jpeg",
  "sha256": "02214b59fe320a6f85f0b429b02658a7accd80fba30d7b12946f72014cf5ec4a",
  "validation": {
    "content_type": "image/jpeg",
    "detected_format": "JPEG",
    "extension": ".jpg",
    "file_size_bytes": 868864,
    "sha256": "02214b59fe320a6f85f0b429b02658a7accd80fba30d7b12946f72014cf5ec4a",
    "width": 1600,
    "height": 1200,
    "mode": "RGB",
    "is_georeferenced": true,
    "warnings": []
  },
  "exif_summary": {
    "gps": { "latitude": 35.6892, "longitude": 51.389, "altitude_m": 120.0, "datum": null, "direction_ref": null },
    "camera": {
      "make": "SampleCam", "model": "SC-4K PRO", "lens_model": null,
      "focal_length_mm": 35.0, "focal_length_35mm_eq": null,
      "exposure_time_s": 0.002, "f_number": 2.8, "iso": 200, "software": "geospatial-mvp"
    },
    "datetime_original": "2024:06:01 09:15:00",
    "orientation": 1,
    "exif_tag_count": 6,
    "has_embedded_xmp": false
  },
  "georeferencing_status": "estimated_from_gps",
  "warnings": [],
  "image_url": "/images/img_8a58ddcd208bc458a8446920/source",
  "next_action": "POST /images/img_8a58ddcd208bc458a8446920/process"
}
```

```bash
# 2. Process
curl -s -X POST http://127.0.0.1:8000/images/img_8a58ddcd208bc458a8446920/process
```

```json
{
  "job_id": "job_8bc95a9e3094162e6b8c257b",
  "image_id": "img_8a58ddcd208bc458a8446920",
  "status": "succeeded",
  "report_url": "/images/img_8a58ddcd208bc458a8446920/jobs/job_8bc95a9e3094162e6b8c257b/report",
  "outputs_url": "/jobs/job_8bc95a9e3094162e6b8c257b/outputs"
}
```

```bash
# 3. Outputs
curl -s http://127.0.0.1:8000/jobs/job_8bc95a9e3094162e6b8c257b/outputs
```

```json
{
  "job_id": "job_8bc95a9e3094162e6b8c257b",
  "image_id": "img_8a58ddcd208bc458a8446920",
  "status": "succeeded",
  "outputs": [
    { "kind": "original", "status": "generated", "filename": "original.jpg", "size_bytes": 868864,
      "download_url": "/images/img_8a58ddcd208bc458a8446920/jobs/job_8bc95a9e3094162e6b8c257b/outputs/original.jpg" },
    { "kind": "metadata_json", "status": "generated", "filename": "metadata.json", … },
    { "kind": "geotiff", "status": "generated", "filename": "image_georeferenced.tif", "size_bytes": 3816859, … },
    { "kind": "cog", "status": "generated", "filename": "image_georeferenced_cog.tif", "size_bytes": 5060003, … },
    { "kind": "preview", "status": "generated", "filename": "preview.jpg", … },
    { "kind": "processing_report", "status": "generated", "filename": "processing_report.json", … }
  ]
}
```

```bash
# 4. Report (abridged)
curl -s http://127.0.0.1:8000/images/img_8a58ddcd208bc458a8446920/jobs/job_8bc95a9e3094162e6b8c257b/report
```

```json
{
  "report_version": "1.0",
  "processing_success": true,
  "geospatial_validity": "valid",
  "geometric_accuracy": "not_assessed",
  "accuracy_statement": "The raster is georeferenced by an ESTIMATE derived from the EXIF GPS fix and camera geometry (nadir assumption). It is NOT an orthophoto and has not been orthorectified. Positional accuracy was not measured.",
  "allocation": {
    "method": "gps_centre_footprint", "crs": "EPSG:32639",
    "pixel_size_x": 0.07714285714285714, "pixel_size_y": 0.07714285714285714,
    "origin_x": 535135.073, "origin_y": 3949593.07, "estimated_gsd": true,
    "notes": ["Georeferencing is an ESTIMATE: …", "…"]
  },
  "geotiff": {
    "driver": "GTiff", "width": 1600, "height": 1200, "band_count": 3, "dtype": "uint8",
    "crs": "EPSG:32639", "pixel_size_x": 0.07714285714285714, "pixel_size_y": 0.07714285714285714,
    "bounds_native": [535135.07, 3949500.5, 535258.5, 3949593.07],
    "bounds_wgs84": [51.3883159, 35.6887805, 51.3896841, 35.6896195],
    "compression": "DEFLATE", "is_cog": false
  },
  "cog": { "driver": "GTiff", "block_size": [512, 512], "overview_levels": [2, 4], "is_cog": true, … },
  "quality_checks": [
    { "name": "file_integrity", "status": "PASS", "observed": 868864, "expected": 868864, "explanation": "…" },
    { "name": "gps_availability", "status": "PASS", "observed": {"latitude": 35.6892, "longitude": 51.389}, "expected": "valid WGS84 coordinate", "explanation": "…" },
    { "name": "georeferencing_availability", "status": "WARNING", "observed": "gps_centre_footprint", "expected": "embedded_geotransform or caller_override", "explanation": "Footprint estimated from GPS fix + camera geometry; it is an approximation." },
    { "name": "cog_validation", "status": "PASS", "observed": {"block_size": [512, 512], "overview_levels": [2, 4], "problems": []}, "…": "…" },
    { "name": "geometric_accuracy", "status": "NOT_APPLICABLE", "observed": "not assessed", "expected": "ground control points or RTK/GNSS reference", "explanation": "…" }
  ],
  "missing_georeferencing_inputs": []
}
```

### Example: image with no georeferencing

`data/samples/aerial_no_geo.png` carries no EXIF at all:

```json
{
  "processing_success": true,
  "geospatial_validity": "not_assessable",
  "geometric_accuracy": "not_assessed",
  "allocation": null,
  "geotiff": null,
  "missing_georeferencing_inputs": ["GPS coordinates", "EXIF focal length"],
  "products": {
    "products": [
      { "kind": "geotiff", "status": "not_generated", "reason": "Missing georeferencing inputs: GPS coordinates, EXIF focal length" },
      { "kind": "cog", "status": "not_generated", "reason": "Missing georeferencing inputs: GPS coordinates, EXIF focal length" },
      { "kind": "preview", "status": "generated", … }
    ]
  }
}
```

### Example: calling process with an explicit placement

```bash
curl -s -X POST http://127.0.0.1:8000/images/img_…/process \
  -H "Content-Type: application/json" \
  -d '{"crs":"EPSG:32639","origin_x":430000.0,"origin_y":3950000.0,"pixel_size_x":0.2,"pixel_size_y":0.2}'
```

All five fields are required together; a partial payload returns `422` with `missing_fields`.
A geographic CRS (e.g. `EPSG:4326`) is rejected for overrides, because a pixel size in degrees is
not a ground resolution.

---

## 5. Output directory layout

```
data/
├── samples/                                  # generated by scripts/make_samples.py
│   ├── aerial_gps.jpg                        # GPS + altitude + focal length
│   ├── aerial_no_geo.png                     # no metadata at all
│   └── aerial_embedded_geotiff.tif           # CRS + transform already embedded
├── uploads/
│   └── img_<id>/
│       ├── image.json                        # validated image record
│       ├── metadata.json                     # full extracted metadata
│       ├── aerial_gps.jpg                    # the stored original
│       └── jobs/
│           └── job_<id>/
│               ├── job.json                  # status + per-step timings
│               ├── original.jpg
│               ├── metadata.json
│               ├── image_georeferenced.tif   # only when georeferenced
│               ├── image_georeferenced_cog.tif
│               ├── preview.jpg
│               └── processing_report.json
└── outputs/                                  # reserved for future cross-job products
```

---

## 6. QC: three different questions

The report deliberately separates three things that are easy to conflate:

| Field | Meaning |
|---|---|
| `processing_success` | Did the pipeline run without crashing? |
| `geospatial_validity` | Is the output honestly placed in a CRS (`valid` / `invalid` / `not_assessable`)? |
| `geometric_accuracy` | Is the placement *correct on the ground*? Always `not_assessed` here — no control points were used. |

Checks return `PASS` / `FAIL` / `WARNING` / `NOT_APPLICABLE` with `observed`, `expected` and an
`explanation`. A job can **succeed** while its georeferencing is only an **estimate** — that is the
normal outcome for a GPS-only image and is reported as such.

GeoTIFF/COG creation failures do not fake success: the product is marked `not_generated` with the
reason, the warning is attached to the report, and the job completes. Truly unexpected errors mark
the job `failed` with `error.code`, `error.message` and the failing step.

---

## 7. Verified end-to-end run

A real run on `data/samples/aerial_gps.jpg` (1600×1200 JPEG, EXIF GPS 35.6892, 51.389 at 120 m,
35 mm focal length), executed through the HTTP API and the browser UI:

- GeoTIFF: EPSG:32639, 1600×1200×3 `uint8`, 0.0771 m/px, DEFLATE-compressed, bounds
  `[535135.07, 3949500.5, 535258.5, 3949593.07]` — independently reopened and read with GDAL.
- COG: same grid, tiled 512×512, overviews `[2, 4]`, identical pixel statistics (min 56 / max 195 /
  mean 119.3), monotonic tile offsets verified from the TIFF IFD chain.
- QC: 16 PASS, 1 WARNING (`georeferencing_availability`), 0 FAIL, 1 NOT_APPLICABLE
  (`geometric_accuracy`).

---

## 8. Known limitations

- **Single image only.** No SfM / dense matching, so no true orthophoto, DSM, DTM, point cloud or
  3D mesh. Those require overlapping imagery and a real photogrammetry engine (ODM etc.).
- **The GPS path is an estimate.** It assumes a nadir view, principal point at image centre, no
  radial distortion, and a 36 mm sensor when the real sensor width is unknown. Expect errors far
  larger than a survey would accept.
- **Not orthorectified.** Terrain relief and camera tilt are not corrected, so away from the image
  centre, position error grows.
- **Synchronous processing with an in-process "running" guard.** Adequate for an MVP; swap in a queue
  and a DB-backed lock before running multiple API workers.
- **PostGIS is not used.** Metadata lives in JSON files; the domain models are ready to persist to
  tables. `data/outputs/` is reserved for cross-job products and is currently unused.
- **Storage is local.** No object storage, cleanup/retention policy, or multi-tenant isolation.
- **Single-image Python 3.14 wheels** are what was tested; the Docker image pins 3.12.
- **No authentication.** Every endpoint is open; put it behind a gateway before exposing it.
- **The map-drawn placement is as accurate as the box you draw** — hand-drawn rectangles are
  convenience georeferencing, not control points.
