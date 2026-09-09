import type {
  ApiErrorPayload,
  RequestOptions,
  SessionPayload,
  StableErrorCode,
} from "./types";

const SESSION_PATH = "/api/miniapp/session";
const REFRESH_PATH = "/api/miniapp/session/refresh";
const REFRESH_WINDOW_MS = 60_000;

const stableCodes = new Set<StableErrorCode>([
  "authentication_required",
  "forbidden",
  "not_found",
  "validation_failed",
  "stale_write",
  "idempotency_key_required",
  "idempotency_conflict",
  "request_in_progress",
  "request_indeterminate",
  "capability_unavailable",
  "secret_already_delivered",
  "internal_error",
  "network",
  "invalid_response",
]);

export class ApiError extends Error {
  constructor(
    public readonly code: StableErrorCode,
    public readonly status: number,
    public readonly retryable: boolean,
    public readonly correlationId?: string,
  ) {
    super(code);
    this.name = "ApiError";
  }
}

export class ApiClient {
  private session?: SessionPayload;
  private authentication?: Promise<SessionPayload>;

  constructor(
    private readonly initData: string,
    private readonly fetcher: typeof fetch = window.fetch.bind(window),
  ) {}

  get locale(): SessionPayload["locale"] | undefined {
    return this.session?.locale;
  }

  async authenticate(): Promise<SessionPayload> {
    if (!this.initData) {
      throw new ApiError("authentication_required", 401, false);
    }
    if (!this.authentication) {
      this.authentication = this.createSession().finally(() => {
        this.authentication = undefined;
      });
    }
    return this.authentication;
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = options.method ?? "GET";
    await this.ensureSession();
    const idempotencyKey =
      method === "POST" ? options.idempotencyKey ?? createRequestId() : undefined;
    return this.send<T>(path, { ...options, method, idempotencyKey }, true);
  }

  private async createSession(): Promise<SessionPayload> {
    const response = await this.fetcher(SESSION_PATH, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-Correlation-ID": createRequestId() },
      body: JSON.stringify({ init_data: this.initData }),
    }).catch(() => {
      throw new ApiError("network", 0, true);
    });
    const payload = await parseResponse<SessionPayload>(response);
    validateSession(payload);
    this.session = payload;
    return payload;
  }

  private async refreshSession(): Promise<SessionPayload> {
    const response = await this.fetcher(REFRESH_PATH, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": this.session?.csrf_token ?? "",
        "X-Idempotency-Key": createRequestId(),
        "X-Correlation-ID": createRequestId(),
      },
      body: "{}",
    }).catch(() => {
      throw new ApiError("network", 0, true);
    });
    const payload = await parseResponse<SessionPayload>(response);
    validateSession(payload);
    this.session = payload;
    return payload;
  }

  private async ensureSession(): Promise<void> {
    if (!this.session) {
      await this.authenticate();
      return;
    }
    const expiresAt = Date.parse(this.session.expires_at);
    if (!Number.isFinite(expiresAt)) {
      this.session = undefined;
      throw new ApiError("invalid_response", 500, false);
    }
    if (expiresAt - Date.now() <= REFRESH_WINDOW_MS) {
      try {
        await this.refreshSession();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          this.session = undefined;
          await this.authenticate();
          return;
        }
        throw error;
      }
    }
  }

  private async send<T>(
    path: string,
    options: RequestOptions & { method: "GET" | "POST"; idempotencyKey?: string },
    canRetry: boolean,
  ): Promise<T> {
    const headers: Record<string, string> = { "X-Correlation-ID": createRequestId() };
    if (options.method === "POST") {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = this.session?.csrf_token ?? "";
      headers["X-Idempotency-Key"] = options.idempotencyKey ?? createRequestId();
    }
    const response = await this.fetcher(path, {
      method: options.method,
      credentials: "include",
      headers,
      body: options.method === "POST" ? JSON.stringify(options.body ?? {}) : undefined,
      signal: options.signal,
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      throw new ApiError("network", 0, true);
    });
    if (response.status === 401 && canRetry && options.retryAuthentication !== false) {
      try {
        await this.refreshSession();
      } catch {
        this.session = undefined;
        await this.authenticate();
      }
      return this.send<T>(path, options, false);
    }
    return parseResponse<T>(response);
  }
}

async function parseResponse<T>(response: Response): Promise<T> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new ApiError("invalid_response", response.status, false);
  }
  if (!response.ok) {
    const raw = isRecord(value) && isRecord(value.error) ? value.error : value;
    const code = isRecord(raw) && isStableCode(raw.code) ? raw.code : errorCodeForStatus(response.status);
    const payload = (isRecord(raw) ? raw : {}) as Partial<ApiErrorPayload>;
    throw new ApiError(
      code,
      response.status,
      payload.retryable === true,
      typeof payload.correlation_id === "string" ? payload.correlation_id : undefined,
    );
  }
  return value as T;
}

function validateSession(payload: SessionPayload): void {
  if (
    !isRecord(payload) ||
    typeof payload.csrf_token !== "string" ||
    payload.csrf_token.length < 8 ||
    typeof payload.expires_at !== "string" ||
    !["ru", "en"].includes(payload.locale)
  ) {
    throw new ApiError("invalid_response", 500, false);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStableCode(value: unknown): value is StableErrorCode {
  return typeof value === "string" && stableCodes.has(value as StableErrorCode);
}

function errorCodeForStatus(status: number): StableErrorCode {
  if (status === 401) return "authentication_required";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "stale_write";
  if (status >= 400 && status < 500) return "validation_failed";
  return "internal_error";
}

export function createRequestId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}
