// 与后端 API 对应的类型定义

export interface StationSeries {
  station_id: string;
  metric: "water_level" | "discharge";
  n: number;
  start_time: string;
  end_time: string;
}

export interface PreviewError {
  row_no: number;
  reason: string;
}

export interface PreviewRow {
  row_no: number;
  station_id: string;
  obs_time: string;
  metric: string;
  value: number;
  unit: string;
}

export interface ImportPreview {
  filename: string;
  file_sha256: string;
  ok: boolean;
  total_rows: number;
  valid_rows: number;
  missing_columns: string[];
  errors: PreviewError[];
  warnings: PreviewError[];
  stations: { station_id: string; metrics: string[] }[];
  preview: PreviewRow[];
  duplicate_import: { id: number; filename: string; imported_at: string } | null;
}

export type RuleCode = "missing" | "duplicate" | "range" | "spike" | "flat";

export interface Issue {
  id: number;
  station_id: string;
  metric: "water_level" | "discharge";
  metric_name: string;
  rule_code: RuleCode;
  rule_label: string;
  severity: string;
  record_id: number | null;
  related_record_id: number | null;
  gap_start: string | null;
  gap_end: string | null;
  expected_count: number | null;
  detail: string;
  status: "open" | "resolved" | "ignored";
  record_time: string | null;
  related_time: string | null;
  effective_value: number | null;
  effective_status: string | null;
}

export interface HydroRecord {
  id: number;
  station_id: string;
  metric: string;
  metric_name: string;
  unit: string | null;
  obs_time: string;
  raw_value: number | null;
  effective_value: number | null;
  effective_status: "valid" | "invalid" | "interpolated";
  quality_flag: string;
  issue_codes: string | null;
  is_interpolated: boolean;
}

export interface InterpSuggestion {
  eligible: boolean;
  reason: string;
  missing_count?: number;
  points?: { obs_time: string; value: number }[];
}

export interface AuditEntry {
  id: number;
  batch_id: string;
  issue_id: number | null;
  record_id: number;
  action: "keep" | "edit" | "invalidate" | "interpolate";
  old_value: number | null;
  new_value: number | null;
  old_status: string;
  new_status: string;
  reason: string;
  created_at: string;
  undone: number;
  obs_time: string;
  station_id: string;
  metric: string;
}

export interface RuleConfig {
  id?: number;
  station_id: string;
  metric: string;
  expected_interval_minutes: number;
  missing_gap_multiplier: number;
  max_gap_intervals: number;
  value_min: number | null;
  value_max: number | null;
  max_change_per_interval: number | null;
  flat_run_minutes: number;
}

export const METRIC_LABEL: Record<string, string> = {
  water_level: "水位",
  discharge: "流量",
};

export const RULE_COLORS: Record<RuleCode, string> = {
  missing: "#0f9b8e",
  duplicate: "#2a78d6",
  range: "#d03b3b",
  spike: "#ec835a",
  flat: "#6b7280",
};
