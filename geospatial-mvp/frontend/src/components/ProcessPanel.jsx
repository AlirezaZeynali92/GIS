import { useState } from "react";
import { api, ApiError } from "../api/client";

export default function ProcessPanel({ upload, placement, onProcessed }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const job = await api.processImage(upload.image_id, placement?.payload);
      const [jobDetail, outputs, report] = await Promise.all([
        api.getJob(job.job_id),
        api.getOutputs(job.job_id),
        api.getReport(upload.image_id, job.job_id),
      ]);
      onProcessed({ job: jobDetail, outputs, report, started: job });
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(String(err)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="hint">
        Runs synchronously: metadata → georeferencing → GeoTIFF/COG (when possible) → preview →
        quality control → report.
      </p>

      <div className="actions">
        <button onClick={run} disabled={busy}>
          {busy && <span className="spinner" />}
          {busy ? "Processing…" : "Start processing"}
        </button>
      </div>

      {placement && (
        <div className="alert info">
          A manual placement will be used: {placement.summary.crs} at{" "}
          {placement.summary.pixelSizeX.toFixed(3)} m/px.
        </div>
      )}

      {error && (
        <div className="alert error">
          <strong>{error.code}</strong>
          {error.status ? ` (HTTP ${error.status})` : ""} — {error.message}
          {error.details && Object.keys(error.details).length > 0 && (
            <div className="mono" style={{ marginTop: 6 }}>
              {JSON.stringify(error.details)}
            </div>
          )}
        </div>
      )}
    </>
  );
}
