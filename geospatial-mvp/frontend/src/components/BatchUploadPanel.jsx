import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";

const ACCEPT = ".jpg,.jpeg,.png,.tif,.tiff,.webp";

export default function BatchUploadPanel({ onUploaded }) {
  const input = useRef(null);
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const submit = async () => {
    if (!files.length) return;
    setBusy(true);
    setError(null);
    try {
      const body = await api.uploadBatch(files);
      setResult(body);
      setFiles([]);
      if (input.current) input.current.value = "";
      onUploaded();
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(String(err)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="dropzone" onClick={() => input.current?.click()} role="button" tabIndex={0}>
        <strong>
          {files.length ? `${files.length} file(s) selected` : "Select several aerial images"}
        </strong>
        <span>Each image is validated on its own; rejected files are listed below.</span>
        <input
          ref={input}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
        />
      </div>

      <div className="actions">
        <button onClick={submit} disabled={!files.length || busy}>
          {busy && <span className="spinner" />}
          {busy ? "Uploading…" : "Upload batch"}
        </button>
      </div>

      {result && (
        <div className="alert info">
          Accepted {result.accepted.length} · georeferenced{" "}
          {result.accepted.filter((item) => item.georeferenced).length} · rejected{" "}
          {result.rejected.length}
          {result.rejected.map((item) => (
            <div key={item.filename} className="mono" style={{ marginTop: 6 }}>
              {item.filename}: {item.message}
            </div>
          ))}
        </div>
      )}

      {error && (
        <div className="alert error">
          <strong>{error.code}</strong> — {error.message}
        </div>
      )}
    </>
  );
}
