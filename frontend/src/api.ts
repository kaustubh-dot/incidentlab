import {
  ApprovalDecision,
  ApprovalView,
  DemoCheckoutResponse,
  DemoFaultResponse,
  DemoResetResponse,
  EventPage,
  EvidenceItem,
  ExecutionView,
  IncidentView,
  StartIncidentRequest,
} from "./types";

export class ApiRequestError extends Error {
  code: string;
  status: number;
  retryable: boolean;

  constructor(message: string, code = "api_error", status = 500, retryable = false) {
    super(message);
    this.name = "ApiRequestError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

async function request<T>(
  path: string,
  options: {
    method?: string;
    token?: string;
    body?: unknown;
  } = {}
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };

  if (options.token) {
    headers["Authorization"] = `Bearer ${options.token}`;
  }

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    let errorCode = "http_error";
    let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
    let retryable = response.status >= 500;

    try {
      const data = await response.json();
      if (data && data.error) {
        errorCode = data.error.code || errorCode;
        errorMessage = data.error.message || errorMessage;
        retryable = Boolean(data.error.retryable);
      } else if (data && data.detail) {
        if (typeof data.detail === "object" && data.detail.error) {
          errorCode = data.detail.error.code || errorCode;
          errorMessage = data.detail.error.message || errorMessage;
          retryable = Boolean(data.detail.error.retryable);
        } else {
          errorMessage = String(data.detail);
        }
      }
    } catch {
      // Non-JSON error body
    }

    throw new ApiRequestError(errorMessage, errorCode, response.status, retryable);
  }

  return response.json() as Promise<T>;
}

export async function demoReset(token: string): Promise<DemoResetResponse> {
  return request<DemoResetResponse>("/api/v1/demo/reset", {
    method: "POST",
    token,
  });
}

export async function demoFault(
  token: string,
  family: string,
  variantSeed = 1
): Promise<DemoFaultResponse> {
  return request<DemoFaultResponse>("/api/v1/demo/fault", {
    method: "POST",
    token,
    body: { family, variant_seed: variantSeed },
  });
}

export async function demoCheckout(
  token: string,
  requestKey?: string
): Promise<DemoCheckoutResponse> {
  return request<DemoCheckoutResponse>("/api/v1/demo/checkout", {
    method: "POST",
    token,
    body: requestKey ? { request_key: requestKey } : {},
  });
}

export async function startIncident(
  token: string,
  payload: StartIncidentRequest
): Promise<IncidentView> {
  return request<IncidentView>("/api/v1/incidents", {
    method: "POST",
    token,
    body: payload,
  });
}

export async function getIncident(token: string, incidentId: string): Promise<IncidentView> {
  return request<IncidentView>(`/api/v1/incidents/${incidentId}`, {
    token,
  });
}

export async function getEvents(
  token: string,
  incidentId: string,
  afterSeq = 0,
  limit = 100
): Promise<EventPage> {
  return request<EventPage>(
    `/api/v1/incidents/${incidentId}/events?after_seq=${afterSeq}&limit=${limit}`,
    {
      token,
    }
  );
}

export async function getEvidence(
  token: string,
  incidentId: string,
  evidenceId: string
): Promise<EvidenceItem> {
  return request<EvidenceItem>(`/api/v1/incidents/${incidentId}/evidence/${evidenceId}`, {
    token,
  });
}

export async function submitDecision(
  token: string,
  incidentId: string,
  decision: ApprovalDecision
): Promise<ApprovalView> {
  return request<ApprovalView>(`/api/v1/incidents/${incidentId}/decision`, {
    method: "POST",
    token,
    body: decision,
  });
}

export async function cancelIncident(token: string, incidentId: string): Promise<IncidentView> {
  return request<IncidentView>(`/api/v1/incidents/${incidentId}/cancel`, {
    method: "POST",
    token,
  });
}

export async function reconcileIncident(
  token: string,
  incidentId: string
): Promise<ExecutionView> {
  return request<ExecutionView>(`/api/v1/incidents/${incidentId}/reconcile`, {
    method: "POST",
    token,
  });
}
