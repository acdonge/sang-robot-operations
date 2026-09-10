export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unknown network error";
}

function responseError(payload: { detail?: unknown; error?: unknown }, fallback: string) {
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((item) => typeof item?.msg === "string" ? item.msg : JSON.stringify(item))
      .join(" · ");
  }
  if (typeof payload.error === "string") return payload.error;
  return fallback;
}

function loginRedirect() {
  window.location.assign(`/login.html?next=${encodeURIComponent(window.location.href)}`);
}

export async function getJson<T>(path: string, redirectOnUnauthorized = true): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (response.status === 401 && redirectOnUnauthorized) {
    loginRedirect();
    throw new Error("Authentication required");
  }
  const payload = await response.json().catch(() => ({})) as T & { detail?: unknown; error?: unknown };
  if (!response.ok) throw new Error(responseError(payload, `${path}: HTTP ${response.status}`));
  return payload;
}

export async function requestJson<T>(method: "POST" | "PUT", path: string, body: unknown): Promise<T> {
  const csrf = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("sang_csrf="))
    ?.split("=", 2)[1];
  const response = await fetch(path, {
    body: JSON.stringify(body),
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}),
    },
    method,
  });
  if (response.status === 401) {
    loginRedirect();
    throw new Error("Authentication required");
  }
  const payload = await response.json().catch(() => ({})) as T & { detail?: unknown; error?: unknown };
  if (!response.ok) throw new Error(responseError(payload, `${path}: HTTP ${response.status}`));
  return payload;
}
