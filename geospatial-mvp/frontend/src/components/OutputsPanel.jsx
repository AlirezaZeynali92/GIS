const LABELS = {
  original: "Original image",
  metadata_json: "Extracted metadata (JSON)",
  geotiff: "Georeferenced GeoTIFF",
  cog: "Cloud Optimized GeoTIFF",
  preview: "Preview (JPEG)",
  processing_report: "Processing report (JSON)",
};

function formatBytes(bytes) {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

export default function OutputsPanel({ result }) {
  const { outputs, report } = result;
  const ordered = [...outputs.outputs].sort(
    (a, b) => Object.keys(LABELS).indexOf(a.kind) - Object.keys(LABELS).indexOf(b.kind),
  );
  const downloadAll = report?.image_id && result.job?.job?.job_id;

  return (
    <>
      <p className="hint">
        Job <span className="mono">{outputs.job_id}</span> · status{" "}
        <strong>{outputs.status}</strong>
      </p>

      <ul className="outputs">
        {ordered.map((item) => (
          <li key={item.kind}>
            <span>
              <span className={`badge ${item.status}`}>{item.status}</span>{" "}
              {LABELS[item.kind] ?? item.kind}
              {item.status === "generated" && (
                <div className="mono" style={{ color: "var(--muted)", marginTop: 2 }}>
                  {item.filename} · {formatBytes(item.size_bytes)}
                </div>
              )}
            </span>
            {item.status === "generated" && item.download_url ? (
              <a className="download" href={item.download_url} download>
                Download
              </a>
            ) : (
              <span className="reason">{item.reason ?? "not available"}</span>
            )}
          </li>
        ))}
      </ul>

      {downloadAll && (
        <div className="actions">
          <a
            className="download"
            href={`/images/${report.image_id}/jobs/${result.job.job.job_id}/report/download`}
            download
          >
            Download QC report file ↗
          </a>
        </div>
      )}
    </>
  );
}
