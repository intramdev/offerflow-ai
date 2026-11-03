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
  first_name: string;
  last_name: string;
}

export interface ProfileUpdate {
  first_name?: string;
  last_name?: string;
  email?: string;
}

export async function updateProfile(update: ProfileUpdate): Promise<SessionUser> {
  return api<SessionUser>("/api/orgs/auth/me/", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
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

export async function requestPasswordReset(email: string): Promise<string> {
  const data = await api<{ detail: string }>("/api/orgs/auth/password/reset/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  return data.detail;
}

export async function confirmPasswordReset(
  uid: string,
  token: string,
  newPassword: string,
): Promise<string> {
  const data = await api<{ detail: string }>(
    "/api/orgs/auth/password/confirm/",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uid,
        token,
        new_password: newPassword,
      }),
    },
  );
  return data.detail;
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
  theme_mode: "system" | "light" | "dark";
  reading_font: "system" | "readable" | "serif" | "mono";
  reading_size: "sm" | "md" | "lg" | "xl";
  contrast_mode: "standard" | "high";
  motion_mode: "system" | "reduce" | "full";
}

export const DEFAULT_PREFERENCES: Preferences = {
  datetime_format: "us",
  theme_mode: "light",
  reading_font: "system",
  reading_size: "md",
  contrast_mode: "standard",
  motion_mode: "system",
};

export async function getPreferences(): Promise<Preferences> {
  return api<Preferences>("/api/orgs/preferences/me/");
}

export async function updatePreferences(
  prefs: Partial<Preferences>,
): Promise<Preferences> {
  return api<Preferences>("/api/orgs/preferences/me/", {
    method: "PATCH",
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

export interface UsageMatchCall extends UsageCall {
  label: string;
  task: string;
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
  imports_count: number;
  matching: UsageMatchCall[];
  matching_count: number;
  log_page_size: number;
  log_retention_days: number;
}

export async function getUsage(organizationId: number): Promise<UsageSummary> {
  return api<UsageSummary>(`/api/orgs/${organizationId}/usage/`);
}

export interface UsageLogPage<T> {
  section: "imports" | "matching";
  page: number;
  pages: number;
  count: number;
  page_size: number;
  results: T[];
}

export interface ReleaseStuckResult {
  swept: { uncertain: number; expired: number; tokens_booked: number };
  live_reservations: {
    id: number;
    state: string;
    tokens: number;
    purpose: string;
    expires_at: string;
  }[];
  in_flight: number;
  tokens_reserved: number;
}

export async function releaseStuckSlots(
  organizationId: number,
): Promise<ReleaseStuckResult> {
  return api(`/api/orgs/${organizationId}/spending/release-stuck/`, {
    method: "POST",
  });
}

export async function getUsageLog(
  organizationId: number,
  section: "imports" | "matching",
  page: number,
): Promise<UsageLogPage<UsageImport | UsageMatchCall>> {
  const params = new URLSearchParams({ section, page: String(page) });
  return api<UsageLogPage<UsageImport | UsageMatchCall>>(
    `/api/orgs/${organizationId}/usage/log/?${params}`,
  );
}

export interface QuoteExportTemplate {
  file_columns: string[];
  message_show_capacity: boolean;
  message_show_grade: boolean;
  message_show_color: boolean;
  message_show_quantity: boolean;
  message_show_categories: boolean;
  message_header: string;
  message_footer: string;
}

export interface OrgSettings {
  default_currency: string;
  destination_currency: string;
  supported_currencies: string[];
  quote_export: QuoteExportTemplate;
}

export async function getOrgSettings(organizationId: number): Promise<OrgSettings> {
  return api<OrgSettings>(`/api/orgs/${organizationId}/settings/`);
}

export async function updateOrgSettings(
  organizationId: number,
  settings: Partial<
    Pick<
      OrgSettings,
      "default_currency" | "destination_currency" | "quote_export"
    >
  >,
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
  source_currency: string;
  fx_destination: string;
  fx_rates: Record<string, string>;
  fx_source: string;
  fx_simulated: boolean;
  fx_fetched_at: string | null;
  category: number | null;
  category_name: string;
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

export async function setFileCategory(
  fileId: number,
  categoryId: number | null,
): Promise<SourceFile> {
  return api<SourceFile>(`/api/pipeline/files/${fileId}/category/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category_id: categoryId }),
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

export interface PreviewSheetEntry {
  name: string;
  kind: "worksheet" | "table" | "text";
  visible: boolean;
  rows: number | null;
  cols: number | null;
}

export interface PreviewSheets {
  available: PreviewSheetEntry[];
  selected: string | null;
  omitted: string[];
  notice: string | null;
}

export interface DraftRecordField {
  value: string | number | null;
  state: "unknown" | "user_confirmed" | "derived" | "model_proposed" | "default_proposed" | "conflicting" | "excluded";
  by: string | null;
  rule: string | null;
  span?: [number, number] | null;
  line?: number | null;
}

export interface DraftRecord {
  id: string;
  lines: number[];
  class: string;
  section: number | null;
  fields: Record<string, DraftRecordField>;
}

export interface DraftRule {
  id?: string;
  kind: string;
  field?: string;
  value?: string | number | null;
  lines?: number[];
  class?: string;
  currency?: string;
  scope?: string | { section_header_line: number };
  header_line?: number;
  header_text?: string;
  label?: string;
  record?: string;
  policy?: string;
  rule?: string;
  reason?: string;
}

export interface DraftIssue {
  scope: "document" | "record";
  field: string;
  record?: string;
  reason: string;
}

export interface DraftSection {
  header_line: number;
  stop: number | null;
  records: string[];
}

export interface DraftHistoryEntry {
  revision: number;
  at: string;
  by: number;
  summary: string;
  operations: AssistOperation[];
}

export interface DraftBlock {
  revision: number;
  document_policy: "stock" | "price_only";
  rules: DraftRule[];
  excluded_lines: number[];
  records: DraftRecord[];
  unresolved: DraftIssue[];
  sections: DraftSection[];
  history: DraftHistoryEntry[];
}

export interface AssistOperation {
  op: string;
  [key: string]: unknown;
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
  message_lines: boolean;
  total_rows: number;
  memory: { use_count: number; last_used_at: string; supplier_hint: string } | null;
  proposed: Record<string, number>;
  proposed_constants: Record<string, string | number>;
  proposed_ignored: string[];
  samples: PreviewSamples;
  sheets: PreviewSheets;
  draft: DraftBlock;
}

export interface MessageCandidate {
  message_candidate: true;
  filename: string;
  text: string;
  truncated: boolean;
  total_chars: number;
  explanation: string;
}

export function isMessageCandidate(
  value: Preview | MessageCandidate,
): value is MessageCandidate {
  return (value as MessageCandidate).message_candidate === true;
}

export async function createPreview(
  organizationId: number,
  file: File,
): Promise<Preview | MessageCandidate> {
  // No explicit Content-Type: the browser sets the multipart boundary.
  const form = new FormData();
  form.append("organization_id", String(organizationId));
  form.append("file", file);
  return api<Preview | MessageCandidate>("/api/pipeline/previews/", { method: "POST", body: form });
}

export async function createPreviewFromText(
  organizationId: number,
  text: string,
): Promise<Preview> {
  return api<Preview>("/api/pipeline/previews/from-text/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId, text }),
  });
}

export interface RenderSwitched {
  headers: HeaderEntry[];
  detected: Record<string, number>;
  missing: string[];
  total_rows: number;
}

export interface RenderResult {
  mapping: Record<string, number>;
  constants: Record<string, string | number>;
  ignored: string[];
  samples: PreviewSamples;
  sheets: PreviewSheets;
  switched?: RenderSwitched;
}

export async function renderPreview(
  key: string,
  mapping: Record<string, number>,
  constants: Record<string, string> = {},
  ignored: string[] = [],
  sheet?: string,
): Promise<RenderResult> {
  return api(`/api/pipeline/previews/${key}/render/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mapping, constants, ignored, sheet }),
  });
}

export async function confirmPreview(
  key: string,
  mapping: Record<string, number>,
  constants: Record<string, string> = {},
  ignored: string[] = [],
  sheet?: string | null,
  revision?: number | null,
): Promise<Run> {
  return api<Run>(`/api/pipeline/previews/${key}/confirm/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mapping, constants, ignored, sheet, revision }),
  });
}

export async function cancelPreview(key: string): Promise<void> {
  await api(`/api/pipeline/previews/${key}/`, { method: "DELETE" });
}

export interface PendingPreview {
  key: string;
  filename: string;
  created_at: string;
  revision: number;
  document_policy: "stock" | "price_only";
  rules_count: number;
  excluded_count: number;
  message_lines: boolean;
  sheet: string | null;
}

export async function listPendingPreviews(
  organizationId: number,
): Promise<PendingPreview[]> {
  const body = await api<{ previews: PendingPreview[] }>(
    `/api/pipeline/previews/pending/?organization_id=${organizationId}`,
  );
  return body.previews;
}

export async function resumePreview(key: string): Promise<Preview> {
  return api<Preview>(`/api/pipeline/previews/${key}/resume/`);
}

export interface PreviewAssistProposal {
  set_constants: Record<string, string>;
  drop_lines: number[];
  dropped_previews: { line: number; text: string }[];
  operations: AssistOperation[];
}

export interface PreviewAssistCoverage {
  scope: string;
  lines_shown: number;
  total_rows: number;
  truncated: boolean;
}

export interface PreviewAssistReply {
  message: string;
  proposal: PreviewAssistProposal;
  base_revision: number;
  context_coverage?: PreviewAssistCoverage;
  tokens: number;
  retriable?: boolean;
  hint?: string;
  refusal?: string;
  replayed?: boolean;
}

export async function assistPreview(
  key: string,
  messages: AssistantTurn[],
  idempotencyKey?: string,
): Promise<PreviewAssistReply> {
  // Bounded client deadline, above the 60s provider timeout plus proxy
  // and validation margin: a hung connection becomes a retryable
  // error, and the retry reuses the idempotency key. When the first
  // attempt completed, the retry replays its stored result without a
  // second charge; after a failed or expired attempt the retry is an
  // explicit new call that may spend again.
  try {
    return await api<PreviewAssistReply>(`/api/pipeline/previews/${key}/assist/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        idempotencyKey ? { messages, idempotency_key: idempotencyKey } : { messages },
      ),
      signal: AbortSignal.timeout(90000),
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(
        "The assistant took too long to answer. Retry to collect the result.",
      );
    }
    throw err;
  }
}

export interface PreviewAssistApplied {
  applied: PreviewAssistProposal & { revision: number };
  preview: Preview;
}

export async function applyPreviewAssist(
  key: string,
  proposal: {
    set_constants: Record<string, string>;
    drop_lines: number[];
    operations?: AssistOperation[];
    revision?: number | null;
    base_revision?: number | null;
  },
): Promise<PreviewAssistApplied> {
  return api<PreviewAssistApplied>(
    `/api/pipeline/previews/${key}/assist/apply/`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(proposal),
    },
  );
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

// --- Categories ---------------------------------------------------------

export interface Category {
  id: number;
  organization: number;
  name: string;
  created_at: string;
}

export async function listCategories(
  organizationId?: number,
): Promise<Category[]> {
  const query = organizationId ? `?organization_id=${organizationId}` : "";
  return api<Category[]>(`/api/pipeline/categories/${query}`);
}

export async function createCategory(
  organizationId: number,
  name: string,
): Promise<Category> {
  return api<Category>("/api/pipeline/categories/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId, name }),
  });
}

export async function renameCategory(
  categoryId: number,
  name: string,
): Promise<Category> {
  return api<Category>(`/api/pipeline/categories/${categoryId}/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function deleteCategory(categoryId: number): Promise<void> {
  await api(`/api/pipeline/categories/${categoryId}/`, { method: "DELETE" });
}

// --- Suppliers --------------------------------------------------------

export interface Supplier {
  id: number;
  organization: number;
  name: string;
  favorite_rank: number | null;
  location: string;
  customs_required: boolean;
  customs_fee_percent: string | null;
  rating_shipping: number | null;
  rating_quality: number | null;
  rating_payment: number | null;
  rating_quantity: number | null;
  rating_rma: number | null;
  notes_count: number;
  created_at: string;
  updated_at: string;
}

export interface SupplierNote {
  id: number;
  supplier: number;
  author_name: string;
  text: string;
  created_at: string;
}

export interface SupplierInput {
  organization_id?: number;
  name?: string;
  location?: string | null;
  customs_required?: boolean;
  customs_fee_percent?: string | null;
  rating_shipping?: number | null;
  rating_quality?: number | null;
  rating_payment?: number | null;
  rating_quantity?: number | null;
  rating_rma?: number | null;
}

export async function listSuppliers(
  organizationId?: number,
): Promise<Supplier[]> {
  const query = organizationId ? `?organization_id=${organizationId}` : "";
  return api<Supplier[]>(`/api/pipeline/suppliers/${query}`);
}

export async function getSupplier(supplierId: number): Promise<Supplier> {
  return api<Supplier>(`/api/pipeline/suppliers/${supplierId}/`);
}

export async function createSupplier(input: SupplierInput): Promise<Supplier> {
  return api<Supplier>("/api/pipeline/suppliers/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function updateSupplier(
  supplierId: number,
  input: SupplierInput,
): Promise<Supplier> {
  return api<Supplier>(`/api/pipeline/suppliers/${supplierId}/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function deleteSupplier(supplierId: number): Promise<void> {
  await api(`/api/pipeline/suppliers/${supplierId}/`, { method: "DELETE" });
}

export async function setFavoriteOrder(
  organizationId: number,
  orderedIds: number[],
): Promise<Supplier[]> {
  return api<Supplier[]>(
    `/api/pipeline/suppliers/order/?organization_id=${organizationId}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ordered_ids: orderedIds }),
    },
  );
}

export async function listSupplierNotes(
  supplierId: number,
): Promise<SupplierNote[]> {
  return api<SupplierNote[]>(`/api/pipeline/suppliers/${supplierId}/notes/`);
}

export async function addSupplierNote(
  supplierId: number,
  text: string,
): Promise<SupplierNote> {
  return api<SupplierNote>(`/api/pipeline/suppliers/${supplierId}/notes/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export async function deleteSupplierNote(
  supplierId: number,
  noteId: number,
): Promise<void> {
  await api(`/api/pipeline/suppliers/${supplierId}/notes/${noteId}/`, {
    method: "DELETE",
  });
}

// --- Quotes -------------------------------------------------------------

export interface QuoteLine {
  id?: string;
  brand: string;
  model: string;
  capacity_gb: number | null;
  grade: string;
  color?: string;
  conditions?: string;
  supplier: string;
  quantity: number | null;
  unit_price: string;
  currency: string;
  rule: string | null;
  manual_price?: boolean;
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

export interface QuoteGroupMember {
  id: number;
  supplier: string;
  model_name: string;
  grade: string;
  color?: string;
  unit_price: string;
  currency: string;
}

export interface QuoteGroup {
  brand: string;
  capacity_gb: number | null;
  grade: string;
  color?: string;
  canonical_model: string;
  confidence: number;
  source: string;
  needs_review: boolean;
  category_id: number | null;
  category_name: string;
  category_source: string;
  category_review: boolean;
  offers: number[];
  suppliers: string[];
  members?: QuoteGroupMember[];
  suggestion?: string;
  category_suggestion?: string;
}

export interface QuoteCandidate {
  supplier: string;
  unit_price: string;
  currency: string;
}

export interface CustomerQuoteLine extends QuoteLine {
  candidates?: QuoteCandidate[];
  category_id: number | null;
  category_name: string;
}

export interface QuoteLineEdit {
  label: string | null;
  hidden: boolean;
  grade?: string;
  color?: string;
  category_id?: number | null;
  category_name?: string;
  quantity?: number | null;
  supplier?: string;
  unit_price?: string;
}

export interface CustomerQuote {
  id: number;
  organization: number;
  status: string;
  source_run_ids: number[];
  sources: { run_id: number; filename: string }[];
  groups: QuoteGroup[];
  lines: CustomerQuoteLine[];
  line_edits: Record<string, QuoteLineEdit>;
  notes: string[];
  message: string;
  provider_name: string;
  simulated: boolean;
  created_at: string;
}

export async function listCustomerQuotes(
  organizationId?: number,
): Promise<CustomerQuote[]> {
  const query = organizationId ? `?organization_id=${organizationId}` : "";
  return api<CustomerQuote[]>(`/api/pipeline/quotes/${query}`);
}

export interface PagedQuotes {
  page: number;
  pages: number;
  count: number;
  page_size: number;
  results: CustomerQuote[];
}

export async function listCustomerQuotesPage(
  organizationId: number,
  page: number,
): Promise<PagedQuotes> {
  return api<PagedQuotes>(
    `/api/pipeline/quotes/?organization_id=${organizationId}&page=${page}`,
  );
}

export async function updateQuoteLines(
  quoteId: number,
  edits: Record<string, QuoteLineEdit>,
): Promise<CustomerQuote> {
  return api<CustomerQuote>(`/api/pipeline/quotes/${quoteId}/lines/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edits }),
  });
}

export async function startCustomerQuote(
  organizationId: number,
): Promise<CustomerQuote> {
  return api<CustomerQuote>(`/api/pipeline/quotes/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId }),
  });
}

export async function getCustomerQuote(quoteId: number): Promise<CustomerQuote> {
  return api<CustomerQuote>(`/api/pipeline/quotes/${quoteId}/`);
}

export async function deleteCustomerQuote(quoteId: number): Promise<void> {
  await api(`/api/pipeline/quotes/${quoteId}/`, { method: "DELETE" });
}

export async function confirmCustomerQuote(
  quoteId: number,
  groups: {
    canonical_model: string;
    offer_ids: number[];
    category_id: number | null;
  }[],
): Promise<CustomerQuote> {
  return api<CustomerQuote>(`/api/pipeline/quotes/${quoteId}/confirm/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ groups }),
  });
}

export async function getCustomerQuoteMessage(
  quoteId: number,
  categoryId?: number | "none",
): Promise<{ message: string }> {
  const query =
    categoryId === undefined ? "" : `?category_id=${categoryId}`;
  return api<{ message: string }>(
    `/api/pipeline/quotes/${quoteId}/message/${query}`,
  );
}

// --- Assistant -----------------------------------------------------------

export async function getAssistantStatus(
  organizationId: number,
): Promise<{ available: boolean }> {
  return api<{ available: boolean }>(
    `/api/pipeline/assistant/status/?organization_id=${organizationId}`,
  );
}

export interface AssistantTurn {
  role: "user" | "assistant";
  content: string;
}

export interface AssistantReply {
  reply: string;
  out_of_scope: boolean;
  tokens: number;
  flagged?: boolean;
}

export async function askAssistant(
  organizationId: number,
  messages: AssistantTurn[],
): Promise<AssistantReply> {
  return api<AssistantReply>("/api/pipeline/assistant/chat/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId, messages }),
  });
}

// --- GDPR ----------------------------------------------------------------

export async function downloadGdprExport(
  organizationId: number,
): Promise<Blob> {
  return download(`/api/orgs/${organizationId}/gdpr/export/`);
}

export interface GdprDeleteResult {
  organization: string;
  stored_files_removed: number;
  member_logins_deleted: number;
}

export async function deleteOrganizationData(
  organizationId: number,
  confirm: string,
): Promise<GdprDeleteResult> {
  return api<GdprDeleteResult>(
    `/api/orgs/${organizationId}/gdpr/delete/`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm }),
    },
  );
}
