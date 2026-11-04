import { api } from "../api/client";

const GEOREF_LABELS = {
  embedded: "Embedded geotransform (CRS + affine transform in file)",
  estimated_from_gps: "Estimated from EXIF GPS + camera geometry",
  override: "Caller-supplied placement",
  missing: "Not georeferenced",
};

export default function ImageSummary({ upload }) {
  const { validation, exif_summary: exif } = upload;
  const gps = exif.gps;

  return (
    <>
      <dl className="kv">
        <dt>Image ID</dt>
        <dd className="mono">{upload.image_id}</dd>
        <dt>Dimensions</dt>
        <dd>
          {validation.width} × {validation.height} px ({validation.mode})
        </dd>
        <dt>Format</dt>
        <dd>
          {validation.detected_format} ({validation.extension})
        </dd>
        <dt>Size</dt>
        <dd>{(validation.file_size_bytes / 1024 / 1024).toFixed(2)} MB</dd>
        <dt>SHA-256</dt>
        <dd className="mono" title={validation.sha256}>
          {validation.sha256.slice(0, 32)}…
        </dd>
        <dt>Georeferencing</dt>
        <dd>{GEOREF_LABELS[upload.georeferencing_status] ?? upload.georeferencing_status}</dd>
        <dt>Camera</dt>
        <dd>
          {exif.camera
            ? [exif.camera.make, exif.camera.model].filter(Boolean).join(" ") || "present"
            : "not available"}
        </dd>
        <dt>Focal length</dt>
        <dd>{exif.camera?.focal_length_mm ? `${exif.camera.focal_length_mm} mm` : "not available"}</dd>
        <dt>GPS fix</dt>
        <dd>
          {gps ? (
            <>
              {gps.latitude.toFixed(6)}, {gps.longitude.toFixed(6)}
              {gps.altitude_m != null ? ` · ${gps.altitude_m.toFixed(1)} m` : " · no altitude"}
            </>
          ) : (
            "not present"
          )}
        </dd>
        <dt>Date taken</dt>
        <dd>{exif.datetime_original ?? "not available"}</dd>
        <dt>EXIF tags</dt>
        <dd>{exif.exif_tag_count}</dd>
      </dl>

      {upload.warnings?.length > 0 && (
        <div className="alert warn">
          <strong>Warnings from validation</strong>
          <ul>
            {upload.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="actions">
        <a className="download" href={api.sourceUrl(upload.image_id)} target="_blank" rel="noreferrer">
          View stored original ↗
        </a>
      </div>
    </>
  );
}
