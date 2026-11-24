export type Project = {
  id: string;
  name: string;
  repo_path: string;
  branch: string;
  git_remote: string;
  yunxiao_entry: string;
  yunxiao_organization_id: string;
  yunxiao_edition: string;
  yunxiao_browser_app: string;
  product_browser: string;
  base_url: string;
  allowed_hosts: string[];
  allowed_databases: string[];
  created_at: string;
  updated_at: string;
  token_configured: boolean;
  current_branch: string | null;
  current_commit: string | null;
  git_error: string | null;
};

export type FieldError = {
  code: string;
  message: string;
};

export type Validation = {
  ok: boolean;
  missing: string[];
  errors: FieldError[];
  dirty: boolean;
  token_configured: boolean;
  ocu_available: boolean;
};

export type RunEvent = {
  time: string;
  status: string;
  step: string;
  basis: string;
};

export type PlanControl = {
  role?: string;
  name?: string;
  locator_status?: string;
};

export type PlanStep = PlanControl & {
  action?: string;
  phase?: string;
  effect?: string;
  value?: string;
  option?: string;
  expect_text?: string[];
};

export type PlanDocument = {
  ui?: {
    seed_object_count?: number;
    step_source?: string;
    code_notes?: Array<{ check_id?: string; text?: string }>;
    pages?: Array<{
      id?: string;
      path?: string;
      title_text?: string;
      controls?: PlanControl[];
    }>;
  };
  cases?: Array<{
    id?: string;
    title?: string;
    goal?: string;
    start?: string;
    origin?: string;
    check_id?: string;
    pk_columns?: string[];
    steps?: PlanStep[];
    expect_text?: string | string[];
  }>;
  checks?: Array<{
    id?: string;
    kind?: string;
    table?: string;
    pk?: string[];
    state_field?: string;
    source?: string;
    allowed_databases?: string[];
    suggestions?: string[];
  }>;
  strategy?: string;
  select_sql?: Record<string, string>;
  records?: Record<string, Array<Record<string, string | null>>>;
  actual?: Record<string, Array<Record<string, string | null>>>;
  seed_steps?: PlanStep[];
  tables?: Array<{ check_id?: string; name?: string; pk?: string[]; state_field?: string }>;
  fixtures?: Record<string, { database?: string; pk?: Record<string, string> }>;
  sql?: string;
  open?: Array<{
    id?: string;
    topic?: string;
    question?: string;
    needs_decision?: boolean;
    decision?: string;
  }>;
};

export type ReviewRecord = {
  id: string;
  digest: string;
  kind: string;
  step: string;
  title: string;
  payload: PlanDocument;
  requested_at: string;
  approved_at?: string;
};

export type RunRecord = {
  id: string;
  project_id: string;
  stage: string;
  status: string;
  stop_reason: string;
  repo_path: string;
  branch: string;
  commit: string;
  exit_code: number | null;
  base_url: string;
  product_browser: string;
  allowed_hosts: string[];
  allowed_databases: string[];
  events: RunEvent[];
  cases: Array<{
    case_id: string;
    title: string;
    status: string;
    seen: string;
    pending: { mark?: string; step_index?: number } | null;
    observed: boolean;
    blocked_reason: string;
  }>;
  current_case: string;
  current_seen: string;
  round_prompt: { rounds: number; question: string } | null;
  requirement: { id: string; title: string; source: string } | null;
  review: ReviewRecord | null;
  approvals: ReviewRecord[];
};

export type RunCreated = {
  run: string;
};

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown) {
    const message = typeof body === "object" && body !== null && "error" in body
      ? String((body as { error: unknown }).error)
      : `请求失败 (${status})`;
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

function token(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="qa-token"]')?.content ?? "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-QA-Token", token());
  if (init.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 200) };
    }
  }
  if (!response.ok) {
    throw new ApiError(response.status, data);
  }
  return data as T;
}
