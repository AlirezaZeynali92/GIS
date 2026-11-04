import { useEffect, useState } from "react";
import { api } from "./api/client";
import UploadPanel from "./components/UploadPanel";
import ImageSummary from "./components/ImageSummary";
import ProcessPanel from "./components/ProcessPanel";
import OutputsPanel from "./components/OutputsPanel";
import ReportPanel from "./components/ReportPanel";
import PlacementMap from "./components/PlacementMap";
import BatchUploadPanel from "./components/BatchUploadPanel";
import CoverageMap from "./components/CoverageMap";

export default function App() {
  const [health, setHealth] = useState({ state: "checking" });
  const [upload, setUpload] = useState(null); // UploadResponse
  const [result, setResult] = useState(null); // { job, outputs, report }
  const [placement, setPlacement] = useState(null); // { crs, origin_x, ... }
  const [coverageVersion, setCoverageVersion] = useState(0);

  useEffect(() => {
    api
      .health()
      .then((body) => setHealth({ state: body.status, checks: body.checks }))
      .catch((error) => setHealth({ state: "down", message: error.message }));
  }, []);

  const onUploaded = (body) => {
    setUpload(body);
    setResult(null);
    setPlacement(null);
  };

  const onProcessed = (payload) => setResult(payload);

  return (
    <div>
      <header className="app-header">
        <div>
          <h1>Geospatial Image Processing</h1>
          <div className="subtitle">Batch upload → coverage map → single-image processing → QC report</div>
        </div>
        <HealthPill health={health} />
      </header>

      <div className="card">
        <h2>Coverage of uploaded imagery</h2>
        <CoverageMap version={coverageVersion} />
      </div>

      <div className="card">
        <h2>Batch upload</h2>
        <BatchUploadPanel onUploaded={() => setCoverageVersion((v) => v + 1)} />
      </div>

      <main className="layout">
        <section>
          <div className="card">
            <h2>
              <span className="step-badge">1</span>Upload aerial image
            </h2>
            <p className="hint">JPEG, PNG, TIFF or WEBP. The file is validated before it is stored.</p>
            <UploadPanel onUploaded={onUploaded} />
          </div>

          {upload && (
            <div className="card">
              <h2>
                <span className="step-badge">2</span>Validate &amp; extract metadata
              </h2>
              <ImageSummary upload={upload} />
            </div>
          )}

          {upload && (
            <div className="card">
              <h2>
                <span className="step-badge">3</span>Georeferencing
              </h2>
              <PlacementMap
                upload={upload}
                placement={placement}
                onPlacementChange={setPlacement}
              />
            </div>
          )}

          {upload && (
            <div className="card">
              <h2>
                <span className="step-badge">4</span>Process
              </h2>
              <ProcessPanel upload={upload} placement={placement} onProcessed={onProcessed} />
            </div>
          )}

          {result && (
            <div className="card">
              <h2>
                <span className="step-badge">5</span>Quality control report
              </h2>
              <ReportPanel result={result} upload={upload} />
            </div>
          )}
        </section>

        <aside>
          <div className="card">
            <h2>Processing outputs</h2>
            {result ? (
              <OutputsPanel result={result} />
            ) : (
              <p className="empty">
                Products appear here after processing. GeoTIFF and COG are only generated when
                the image genuinely carries georeferencing.
              </p>
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}

function HealthPill({ health }) {
  if (health.state === "checking") {
    return <span className="health-pill">Checking API…</span>;
  }
  if (health.state === "down") {
    return (
      <span className="health-pill bad" title={health.message}>
        API unreachable
      </span>
    );
  }
  return (
    <span className="health-pill ok" title={JSON.stringify(health.checks)}>
      API {health.state} · GDAL {health.checks?.gdal ?? "?"}
    </span>
  );
}
