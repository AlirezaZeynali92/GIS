import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";

const ACCEPT = ".jpg,.jpeg,.png,.tif,.tiff,.webp";

export default function UploadPanel({ onUploaded }) {
  const fileInput = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState(null);
  const [progress, setProgress] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const pick = (candidate) => {
    setError(null);
    setFile(candidate ?? null);
    setProgress(0);
  };

  const onDrop = (event) => {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files?.[0];
    if (dropped) pick(dropped);
  };

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const body = await api.uploadImage(file, setProgress);
      onUploaded(body);
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(String(err)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div
        className={`dropzone${dragging ? " active" : ""}`}
        onClick={() => fileInput.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onKeyDown={(event) => event.key === "Enter" && fileInput.current?.click()}
        role="button"
        tabIndex={0}
      >
        <strong>Drop an aerial image here, or click to browse</strong>
        <span>Max 150 MB. Formats: JPEG, PNG, TIFF, WEBP.</span>
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT}
          hidden
          onChange={(event) => pick(event.target.files?.[0])}
        />
      </div>

      {file && (
        <div className="file-row">
          <span>
            <strong>{file.name}</strong> · {(file.size / 1024 / 1024).toFixed(2)} MB
          </span>
          {busy ? <span className="mono">{progress}%</span> : <button className="link" onClick={() => pick(null)}>Remove</button>}
        </div>
      )}

      <div className="actions">
        <button onClick={submit} disabled={!file || busy}>
          {busy && <span className="spinner" />}
          {busy ? "Uploading…" : "Upload & validate"}
        </button>
      </div>

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
