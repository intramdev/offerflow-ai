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

export async function download(path: string): Promise<Blob> {
  const response = await fetch(path, { credentials: "include" });
  if (!response.ok) {
    const data = (await response.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    throw new ApiError(errorMessage(data, response.status), response.status);
  }
  return response.blob();
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// --- Auth ---------------------------------------------------------------

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

// --- Organizations ------------------------------------------------------

export interface Membership {
  id: number;
  organization: number;
  organization_name: string;
  role: string;
  joined_at: string;
}

export async function listMemberships(): Promise<Membership[]> {
  return api<Membership[]>("/api/orgs/memberships/");
}

export async function getActiveOrg(): Promise<Membership | { organization: null }> {
  return api<Membership | { organization: null }>("/api/orgs/active/");
}

export async function setActiveOrg(organizationId: number): Promise<Membership> {
  return api<Membership>("/api/orgs/active/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId }),
  });
}

export interface Preferences {
  datetime_format: "us" | "eu";
}

export async function getPreferences(): Promise<Preferences> {
  return api<Preferences>("/api/orgs/preferences/me/");
}

export async function updatePreferences(prefs: Preferences): Promise<Preferences> {
  return api<Preferences>("/api/orgs/preferences/me/", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(prefs),
  });
}

export interface UsageCall {
  at: string;
  prompt_tokens: number;
  completion_tokens: number;
  total: number;
  model: string;
}

export interface UsageImport {
  run_id: number;
  filename: string;
  created_at: string;
  run_status: string;
  requests: number;
  tokens: number;
  calls: UsageCall[];
}

export interface UsageSummary {
  allowance: number;
  used: number;
  remaining: number;
  period_start: string;
  reset_date: string;
  grand_total: number;
  paused: boolean;
  imports: UsageImport[];
}

export async function getUsage(organizationId: number): Promise<UsageSummary> {
  return api<UsageSummary>(`/api/orgs/${organizationId}/usage/`);
}

export interface OrgSettings {
  default_currency: string;
  destination_currency: string;
}

export async function getOrgSettings(organizationId: number): Promise<OrgSettings> {
  return api<OrgSettings>(`/api/orgs/${organizationId}/settings/`);
}

export async function updateOrgSettings(
  organizationId: number,
  settings: Partial<OrgSettings>,
): Promise<OrgSettings> {
  return api<OrgSettings>(`/api/orgs/${organizationId}/settings/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

// --- Runs ---------------------------------------------------------------

export interface Run {
  id: number;
  organization: number;
  status: string;
  provider_name: string;
  simulated: boolean;
  error: string;
  mapping: Record<string, unknown>;
  rule_version: string;
  approved_at: string | null;
  counts: { rows: number; offers: number; open_reviews: number; artifacts: number };
  artifacts: { id: number; format: string; sha256: string; size_bytes: number; created_at: string }[];
  validations?: { name: string; passed: boolean; message: string; details: unknown; created_at: string }[];
}

export async function getRun(runId: number): Promise<Run> {
  return api<Run>(`/api/pipeline/runs/${runId}/`);
}

export async function approveRun(runId: number): Promise<Run> {
  return api<Run>(`/api/pipeline/runs/${runId}/approve/`, { method: "POST" });
}

export async function cancelRun(runId: number): Promise<Run> {
  return api<Run>(`/api/pipeline/runs/${runId}/cancel/`, { method: "POST" });
}

export async function remapRun(
  runId: number,
  mapping: Record<string, number>,
  constants: Record<string, string> = {},
  ignored: string[] = [],
): Promise<Run> {
  return api<Run>(`/api/pipeline/runs/${runId}/remap/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mapping, constants, ignored }),
  });
}

export interface Offer {
  id: number;
  source_row: number;
  supplier: string;
  brand: string;
  model_name: string;
  capacity_gb: number | null;
  color: string;
  grade: string;
  lock_state: string;
  quantity: number | null;
  unit_price: string | null;
  currency: string;
  conditions: string;
  status: string;
  exclusion_reason: string;
}

export async function listOffers(runId: number): Promise<Offer[]> {
  return api<Offer[]>(`/api/pipeline/runs/${runId}/offers/`);
}

export interface Review {
  id: number;
  offer: number | null;
  kind: string;
  field: string;
  question: string;
  evidence: unknown;
  choices: string[];
  status: string;
  created_at: string;
}

export async function listReviews(runId: number): Promise<Review[]> {
  return api<Review[]>(`/api/pipeline/runs/${runId}/reviews/`);
}

export async function answerReview(
  reviewId: number,
  answer: { choice?: string; value?: unknown },
): Promise<{ review: number; decision: number; run_status: string }> {
  return api(`/api/pipeline/reviews/${reviewId}/answer/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(answer),
  });
}

export async function ignoreReview(
  reviewId: number,
): Promise<{ review: number; offer: number; run_status: string }> {
  return api(`/api/pipeline/reviews/${reviewId}/ignore/`, {
    method: "POST",
  });
}

export async function exportRun(
  runId: number,
  format: "xlsx" | "csv",
): Promise<{ id: number }> {
  return api(`/api/pipeline/runs/${runId}/export/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ format }),
  });
}

// --- Files --------------------------------------------------------------

export interface SourceFile {
  id: number;
  organization: number;
  organization_name: string;
  original_name: string;
  size_bytes: number;
  sha256: string;
  run_id: number | null;
  run_status: string | null;
  uploaded_by_name: string | null;
  currency_date: string;
  fx_destination: string;
  fx_rates: Record<string, string>;
  fx_source: string;
  fx_simulated: boolean;
  fx_fetched_at: string | null;
  created_at: string;
}

export async function listFiles(organizationId?: number): Promise<SourceFile[]> {
  const query = organizationId ? `?organization_id=${organizationId}` : "";
  return api<SourceFile[]>(`/api/pipeline/files/${query}`);
}

export async function renameFile(
  fileId: number,
  originalName: string,
): Promise<SourceFile> {
  return api<SourceFile>(`/api/pipeline/files/${fileId}/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ original_name: originalName }),
  });
}

export async function deleteFile(fileId: number): Promise<void> {
  await api(`/api/pipeline/files/${fileId}/`, { method: "DELETE" });
}

export async function updateFileFx(
  fileId: number,
  currencyDate: string,
): Promise<SourceFile> {
  return api<SourceFile>(`/api/pipeline/files/${fileId}/fx/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ currency_date: currencyDate }),
  });
}

// --- Previews -----------------------------------------------------------

export interface HeaderEntry {
  position: number;
  letter: string;
  header: string;
}

export interface SampleRow {
  index: number;
  cells: Record<string, unknown>;
  normalized?: Record<string, unknown>;
  status?: string;
  issues?: string[];
}

export interface PreviewSamples {
  complete: boolean;
  missing: string[];
  detected: Record<string, number>;
  sources: Record<string, string>;
  applied_defaults?: Record<string, string>;
  rows: SampleRow[];
}

export interface Preview {
  key: string;
  filename: string;
  extension: string;
  size_bytes: number;
  sha256: string;
  headers: HeaderEntry[];
  detected: Record<string, number>;
  sources: Record<string, string>;
  missing: string[];
  memory: { use_count: number; last_used_at: string; supplier_hint: string } | null;
  proposed: Record<string, number>;
  proposed_constants: Record<string, string | number>;
  proposed_ignored: string[];
  samples: PreviewSamples;
}

export async function createPreview(
  organizationId: number,
  file: File,
): Promise<Preview> {
  // No explicit Content-Type: the browser sets the multipart boundary.
  const form = new FormData();
  form.append("organization_id", String(organizationId));
  form.append("file", file);
  return api<Preview>("/api/pipeline/previews/", { method: "POST", body: form });
}

export interface RenderResult {
  mapping: Record<string, number>;
  constants: Record<string, string | number>;
  ignored: string[];
  samples: PreviewSamples;
}

export async function renderPreview(
  key: string,
  mapping: Record<string, number>,
  constants: Record<string, string> = {},
  ignored: string[] = [],
): Promise<RenderResult> {
  return api(`/api/pipeline/previews/${key}/render/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mapping, constants, ignored }),
  });
}

export async function confirmPreview(
  key: string,
  mapping: Record<string, number>,
  constants: Record<string, string> = {},
  ignored: string[] = [],
): Promise<Run> {
  return api<Run>(`/api/pipeline/previews/${key}/confirm/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mapping, constants, ignored }),
  });
}

export async function cancelPreview(key: string): Promise<void> {
  await api(`/api/pipeline/previews/${key}/`, { method: "DELETE" });
}

// --- Pricing rules ------------------------------------------------------

export interface PricingRule {
  id: number;
  organization: number;
  name: string;
  matcher: Record<string, string>;
  supplier_strategy: "lowest" | "highest" | "preferred";
  preferred_supplier: string;
  ops: Record<string, string>[];
  priority: number;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export type RuleInput = Pick<
  PricingRule,
  | "name"
  | "matcher"
  | "supplier_strategy"
  | "preferred_supplier"
  | "ops"
  | "priority"
  | "active"
> & { organization_id?: number };

export async function listRules(organizationId?: number): Promise<PricingRule[]> {
  const query = organizationId ? `?organization_id=${organizationId}` : "";
  return api<PricingRule[]>(`/api/pipeline/rules/${query}`);
}

export async function createRule(input: RuleInput): Promise<PricingRule> {
  return api<PricingRule>("/api/pipeline/rules/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function updateRule(
  ruleId: number,
  input: Partial<RuleInput>,
): Promise<PricingRule> {
  return api<PricingRule>(`/api/pipeline/rules/${ruleId}/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function deleteRule(ruleId: number): Promise<void> {
  await api(`/api/pipeline/rules/${ruleId}/`, { method: "DELETE" });
}

// --- Quotes -------------------------------------------------------------

export interface QuoteLine {
  brand: string;
  model: string;
  capacity_gb: number | null;
  grade: string;
  supplier: string;
  quantity: number | null;
  unit_price: string;
  currency: string;
  rule: string | null;
}

export interface QuoteFx {
  currency_date: string;
  destination: string | null;
  rates: Record<string, string>;
  source: string | null;
  simulated: boolean;
  fetched_at: string | null;
}

export interface Quote {
  run_id: number;
  run_status: string;
  lines: QuoteLine[];
  notes: string[];
  fx: QuoteFx;
}

export async function getQuote(runId: number): Promise<Quote> {
  return api<Quote>(`/api/pipeline/runs/${runId}/quote/`);
}
