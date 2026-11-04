import { useState } from "react";

function formatObserved(value) {
  if (value == null) return "—";
  if (Array.isArray(value)) return value.map((v) => (typeof v === "number" ? Number(v.toFixed(6)) : String(v))).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "number") return Number(value.toFixed(6)).toString();
  return String(value);
}

export default function ReportPanel({ result }) {
  const { report, job } = result;
  const [tab, setTab] = useState("checks");

  const summary = report.quality_checks.reduce(
    (acc, check) => ({ ...acc, [check.status]: (acc[check.status] ?? 0) + 1 }),
    {},
  );

  return (
    <>
      <dl className="tri-state">
        <div>
          <dt>Processing success</dt>
          <dd style={{ color: report.processing_success ? "var(--pass)" : "var(--fail)" }}>
            {report.processing_success ? "Succeeded" : "Failed"}
          </dd>
        </div>
        <div>
          <dt>Geospatial validity</dt>
          <dd style={{ color: report.geospatial_validity === "valid" ? "var(--pass)" : "var(--warn)" }}>
            {report.geospatial_validity}
          </dd>
        </div>
        <div>
          <dt>Geometric accuracy</dt>
          <dd style={{ color: "var(--na)" }}>{report.geometric_accuracy}</dd>
        </div>
      </dl>

      <div className="alert warn">
        <strong>Accuracy statement.</strong> {report.accuracy_statement}
      </div>

      {report.missing_georeferencing_inputs?.length > 0 && (
        <div className="alert error">
          <strong>Missing georeferencing inputs</strong>
          <ul>
            {report.missing_georeferencing_inputs.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="tabs" style={{ marginTop: 16 }}>
        {["checks", "raster", "notes", "json"].map((name) => (
          <button
            key={name}
            className={tab === name ? "active" : ""}
            onClick={() => setTab(name)}
          >
            {name === "checks"
              ? `QC checks (${report.quality_checks.length})`
              : name === "raster"
                ? "Raster metadata"
                : name === "notes"
                  ? "Notes & warnings"
                  : "Raw JSON"}
          </button>
        ))}
      </div>

      {tab === "checks" && (
        <>
          <div className="summary-row">
            {["PASS", "WARNING", "FAIL", "NOT_APPLICABLE"].map((status) => (
              <span key={status} className={`badge ${status}`}>
                {status.replace("_", " ")}: {summary[status] ?? 0}
              </span>
            ))}
          </div>
          <table className="qc">
            <thead>
              <tr>
                <th>Check</th>
                <th>Status</th>
                <th>Observed</th>
                <th>Expected</th>
              </tr>
            </thead>
            <tbody>
              {report.quality_checks.map((check) => (
                <tr key={check.name}>
                  <td title={check.explanation}>{check.name}</td>
                  <td>
                    <span className={`badge ${check.status}`}>{check.status}</span>
                  </td>
                  <td className="observed">{formatObserved(check.observed)}</td>
                  <td className="observed">{formatObserved(check.expected)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {tab === "raster" && (
        <dl className="kv">
          <dt>GeoTIFF</dt>
          <dd>{report.geotiff ? "generated" : "not generated"}</dd>
          {report.geotiff && (
            <>
              <dt>Size</dt>
              <dd>
                {report.geotiff.width} × {report.geotiff.height} × {report.geotiff.band_count} bands
              </dd>
              <dt>CRS</dt>
              <dd>{report.geotiff.crs}</dd>
              <dt>Pixel size</dt>
              <dd>
                {report.geotiff.pixel_size_x} × {report.geotiff.pixel_size_y} CRS units
              </dd>
              <dt>Bounds (WGS84)</dt>
              <dd className="mono">
                {(report.geotiff.bounds_wgs84 ?? report.geotiff.bounds_native)
                  .map((v) => v.toFixed(6))
                  .join(", ")}
              </dd>
              <dt>NoData</dt>
              <dd>{report.geotiff.nodata ?? "none"}</dd>
              <dt>Compression</dt>
              <dd>{report.geotiff.compression ?? "—"}</dd>
            </>
          )}
          <dt>COG</dt>
          <dd>{report.cog ? "generated" : "not generated"}</dd>
          {report.cog && (
            <>
              <dt>Blocks</dt>
              <dd>{report.cog.block_size?.join(" × ")}</dd>
              <dt>Overview levels</dt>
              <dd>{report.cog.overview_levels.join(", ") || "none"}</dd>
            </>
          )}
          <dt>Placement method</dt>
          <dd>{report.allocation?.method ?? "none"}</dd>
          <dt>GSD estimated?</dt>
          <dd>{report.allocation ? (report.allocation.estimated_gsd ? "yes" : "no") : "—"}</dd>
        </dl>
      )}

      {tab === "notes" && (
        <>
          {report.warnings?.length > 0 && (
            <div className="alert warn">
              <strong>Warnings</strong>
              <ul>
                {report.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
          )}
          {report.notes?.length > 0 && (
            <div className="alert info">
              <strong>Georeferencing notes</strong>
              <ul>
                {report.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </div>
          )}
          {!report.warnings?.length && !report.notes?.length && (
            <p className="empty">No warnings or notes.</p>
          )}
        </>
      )}

      {tab === "json" && (
        <pre className="json">{JSON.stringify({ job: job.job, report }, null, 2)}</pre>
      )}
    </>
  );
}
