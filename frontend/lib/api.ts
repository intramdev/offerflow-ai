// Same-origin API client with Django session + CSRF handling.

function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document
    .cookie.split("; ")
    .find((entry) => entry.startsWith(name + "="));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

async function ensureCsrf(): Promise<string | null> {
  let token = getCookie("csrftoken");
  if (!token) {
    await fetch("/api/orgs/auth/csrf/", { credentials: "include" });
    token = getCookie("csrftoken");
  }
  return token;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function errorMessage(data: Record<string, unknown>, status: number): string {
  const detail = data.detail ?? data.Detail;
  if (typeof detail === "string") return detail;
  const fields = Object.entries(data)
    .filter(([, value]) => Array.isArray(value))
    .map(([key, value]) => `${key}: ${(value as unknown[]).join(", ")}`);
  if (fields.length > 0) return fields.join("; ");
  return `Request failed (${status})`;
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = await ensureCsrf();
    if (token) headers.set("X-CSRFToken", token);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "include",
  });
  if (response.status === 204) return undefined as T;
  const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    throw new ApiError(errorMessage(data, response.status), response.status);
  }
  return data as T;
}

export interface SessionUser {
  id: number;
  username: string;
  email: string;
}

export async function login(
  username: string,
  password: string,
): Promise<SessionUser> {
  // The token rotates on login; the browser stores the fresh cookie from
  // the login response itself, so later calls just re-read it.
  return api<SessionUser>("/api/orgs/auth/login/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export async function logout(): Promise<void> {
  await api("/api/orgs/auth/logout/", { method: "POST" });
}

export async function me(): Promise<SessionUser> {
  return api<SessionUser>("/api/orgs/auth/me/");
}

export interface Run {
  id: number;
  status: string;
}

export async function uploadRun(
  organizationId: number,
  file: File,
): Promise<Run> {
  // No explicit Content-Type: the browser sets the multipart boundary.
  const form = new FormData();
  form.append("organization_id", String(organizationId));
  form.append("file", file);
  return api<Run>("/api/pipeline/uploads/", { method: "POST", body: form });
}
