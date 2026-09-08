// API 封装：统一错误处理

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string"
        ? body.detail
        : body.detail?.message || JSON.stringify(body.detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  stations: () => request<import("./types").StationSeries[]>("/api/stations"),
  configs: (stationId?: string) =>
    request<import("./types").RuleConfig[]>(
      `/api/configs${stationId ? `?station_id=${stationId}` : ""}`),
  saveConfig: (cfg: import("./types").RuleConfig) =>
    request<{ saved: boolean }>("/api/configs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    }),
  issues: (params: Record<string, string>) =>
    request<import("./types").Issue[]>(`/api/issues?${new URLSearchParams(params)}`),
  records: (station: string, metric: string, start?: string, end?: string) => {
    const p = new URLSearchParams({ station_id: station, metric });
    if (start) p.set("start", start);
    if (end) p.set("end", end);
    return request<{ records: import("./types").HydroRecord[] }>(
      `/api/records?${p}`);
  },
  action: (body: Record<string, unknown>) =>
    request<{ batch_id: string }>("/api/actions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  undo: (batchId: string) =>
    request<{ undone_batch: string }>("/api/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: batchId }),
    }),
  interpSuggestion: (issueId: number) =>
    request<import("./types").InterpSuggestion>(`/api/issues/${issueId}/interpolation`),
  interpolate: (issueId: number, reason: string) =>
    request<{ batch_id: string }>(`/api/issues/${issueId}/interpolate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    }),
  audit: () => request<import("./types").AuditEntry[]>("/api/audit"),
  exportUrl: (params: Record<string, string>) =>
    `/api/export?${new URLSearchParams(params)}`,
};
