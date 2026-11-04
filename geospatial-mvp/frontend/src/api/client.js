/**
 * Thin API client for the FastAPI backend.
 *
 * Every call goes through `request()` so structured backend errors
 * (`{ error: { code, message, details } }`) surface as `ApiError` instead of
 * silently producing `undefined` in the UI.
 */

const JSON_HEADERS = { Accept: "application/json" };

export class ApiError extends Error {
  constructor(message, { status, code, details } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status ?? 0;
    this.code = code ?? "network_error";
    this.details = details ?? {};
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError("Cannot reach the API. Is the backend running on port 8000?", {
      code: "connection_failed",
      details: { path },
    });
  }

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : null;

  if (!response.ok) {
    const err = payload?.error ?? {};
    const validationText =
      err.code === "request_validation_failed"
        ? JSON.stringify(err.details?.errors ?? []).slice(0, 300)
        : "";
    const base = err.message || `Request failed with HTTP ${response.status}`;
    throw new ApiError(validationText ? `${base} — ${validationText}` : base, {
      status: response.status,
      code: err.code,
      details: err.details,
    });
  }
  return payload;
}

export const api = {
  health: () => request("/health", { headers: JSON_HEADERS }),

  uploadImage(file, onProgress) {
    // fetch() has no upload progress; XMLHttpRequest is the pragmatic choice here.
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", file, file.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/images/upload");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(Math.round((event.loaded / event.total) * 100));
        }
      });
      xhr.addEventListener("load", () => {
        let body = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          body = null;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body);
          return;
        }
        const err = body?.error ?? {};
        reject(
          new ApiError(err.message || `Upload failed with HTTP ${xhr.status}`, {
            status: xhr.status,
            code: err.code,
            details: err.details,
          }),
        );
      });
      xhr.addEventListener("error", () =>
        reject(new ApiError("Network error during upload", { code: "connection_failed" })),
      );
      xhr.send(form);
    });
  },

  uploadBatch(files) {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    return request("/images/batch", { method: "POST", headers: JSON_HEADERS, body: form });
  },

  listFootprints: () => request("/images/footprints", { headers: JSON_HEADERS }),

  getImage: (imageId) => request(`/images/${imageId}`, { headers: JSON_HEADERS }),

  getMetadata: (imageId) => request(`/images/${imageId}/metadata`, { headers: JSON_HEADERS }),

  processImage: (imageId, placement) =>
    request(`/images/${imageId}/process`, {
      method: "POST",
      headers: { ...JSON_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(placement ?? {}),
    }),

  getJob: (jobId) => request(`/jobs/${jobId}`, { headers: JSON_HEADERS }),

  getOutputs: (jobId) => request(`/jobs/${jobId}/outputs`, { headers: JSON_HEADERS }),

  getReport: (imageId, jobId) =>
    request(`/images/${imageId}/jobs/${jobId}/report`, { headers: JSON_HEADERS }),

  reportDownloadUrl: (imageId, jobId) => `/images/${imageId}/jobs/${jobId}/report/download`,

  sourceUrl: (imageId) => `/images/${imageId}/source`,
};
