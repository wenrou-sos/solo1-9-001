import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type {
  AuditEntry, HydroRecord, ImportPreview, Issue, RuleConfig, StationSeries,
} from "./types";
import { METRIC_LABEL, RULE_COLORS } from "./types";
import TimeChart from "./TimeChart";

type Tab = "workbench" | "import" | "config" | "history";

export default function App() {
  const [tab, setTab] = useState<Tab>("workbench");
  const [toast, setToast] = useState<{ msg: string; error?: boolean } | null>(null);
  const showToast = (msg: string, error = false) => {
    setToast({ msg, error });
    setTimeout(() => setToast(null), 3500);
  };

  return (
    <>
      <header className="app-header">
        <h1>水文监测数据校核工作台</h1>
        <nav className="app-nav">
          <button className={tab === "workbench" ? "active" : ""}
            onClick={() => setTab("workbench")}>校核工作台</button>
          <button className={tab === "import" ? "active" : ""}
            onClick={() => setTab("import")}>数据导入</button>
          <button className={tab === "config" ? "active" : ""}
            onClick={() => setTab("config")}>规则配置</button>
          <button className={tab === "history" ? "active" : ""}
            onClick={() => setTab("history")}>处理记录 / 撤销</button>
        </nav>
      </header>
      <main className="page">
        {tab === "workbench" && <Workbench showToast={showToast} />}
        {tab === "import" && <ImportPage showToast={showToast} />}
        {tab === "config" && <ConfigPage showToast={showToast} />}
        {tab === "history" && <HistoryPage showToast={showToast} />}
      </main>
      {toast && <div className={`toast${toast.error ? " error" : ""}`}>{toast.msg}</div>}
    </>
  );
}

// ---------------- 数据导入 ----------------

function ImportPage({ showToast }: { showToast: (m: string, e?: boolean) => void }) {
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [committing, setCommitting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const onFile = async () => {
    const f = fileRef.current?.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const res = await fetch("/api/import/preview", { method: "POST", body: fd });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || "预览失败");
      setPreview(body);
    } catch (e) {
      showToast((e as Error).message, true);
    }
  };

  const commit = async (force: boolean) => {
    const f = fileRef.current?.files?.[0];
    if (!f) return;
    setCommitting(true);
    try {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("force", String(force));
      const res = await fetch("/api/import/commit", { method: "POST", body: fd });
      const body = await res.json();
      if (!res.ok) {
        if (res.status === 409) {
          showToast(body.detail?.message || "文件已导入过", true);
          return;
        }
        throw new Error(body.detail?.message || "导入失败");
      }
      showToast(`导入成功：${body.row_count} 行，校核发现 ${body.check.issues_upserted} 条问题`);
      setPreview(null);
      if (fileRef.current) fileRef.current.value = "";
    } catch (e) {
      showToast((e as Error).message, true);
    } finally {
      setCommitting(false);
    }
  };

  return (
    <div className="card">
      <h2>数据导入（CSV）</h2>
      <p style={{ color: "var(--text-secondary)", margin: "0 0 12px" }}>
        必填列：<b>站点编号, 监测时间, 监测指标, 数值, 单位</b>；指标支持“水位/water_level”
        “流量/discharge”。导入前先预览，字段、时间、数值错误会定位到具体行。
      </p>
      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <a className="btn secondary" style={{ textDecoration: "none", display: "inline-block" }}
          href="/api/template">下载 CSV 模板</a>
        <input ref={fileRef} type="file" accept=".csv" onChange={onFile} />
      </div>

      {preview && (
        <div style={{ marginTop: 16 }}>
          <div className="stat-row">
            <div className="stat-tile"><div className="num">{preview.total_rows}</div>
              <div className="label">数据行</div></div>
            <div className="stat-tile"><div className="num">{preview.valid_rows}</div>
              <div className="label">可导入行</div></div>
            <div className="stat-tile"><div className="num" style={{ color: "var(--status-critical)" }}>
              {preview.errors.length}</div><div className="label">错误（阻断）</div></div>
            <div className="stat-tile"><div className="num" style={{ color: "var(--status-warning)" }}>
              {preview.warnings.length}</div><div className="label">警告（重复行，导入后校核标出）</div></div>
          </div>

          {preview.duplicate_import && (
            <div className="error-box">
              <b>⚠ 重复文件提醒：</b>该文件内容与 {preview.duplicate_import.filename}
              （{preview.duplicate_import.imported_at} 导入）完全相同。
              重复导入会产生一批重复记录，请确认后再强制导入。
              <div style={{ marginTop: 8 }}>
                <button className="btn danger small" disabled={committing || !preview.ok}
                  onClick={() => commit(true)}>我已知晓，仍要强制导入</button>
              </div>
            </div>
          )}

          {preview.errors.length > 0 && (
            <div className="error-box">
              <b>校验错误（{preview.errors.length}），无法导入：</b>
              {preview.errors.slice(0, 30).map((e, i) => (
                <div className="row" key={i}>
                  {e.row_no > 0 && <b>第 {e.row_no} 行：</b>}{e.reason}
                </div>
              ))}
              {preview.errors.length > 30 && <div className="row">… 其余 {preview.errors.length - 30} 条略</div>}
            </div>
          )}
          {preview.warnings.length > 0 && (
            <div className="warning-box">
              <b>警告（{preview.warnings.length}）：</b>
              {preview.warnings.map((w, i) => (
                <div className="row" key={i}><b>第 {w.row_no} 行：</b>{w.reason}</div>
              ))}
            </div>
          )}

          <h3>数据预览（前 50 行可导入数据）</h3>
          <table className="records">
            <thead><tr><th>CSV 行</th><th>站点</th><th>监测时间</th><th>指标</th>
              <th>数值</th><th>单位</th></tr></thead>
            <tbody>
              {preview.preview.map((r) => (
                <tr key={r.row_no}>
                  <td>{r.row_no}</td><td>{r.station_id}</td><td>{r.obs_time}</td>
                  <td>{METRIC_LABEL[r.metric] || r.metric}</td>
                  <td>{r.value}</td><td>{r.unit}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div style={{ marginTop: 14, display: "flex", gap: 10 }}>
            <button className="btn" disabled={!preview.ok || committing || !!preview.duplicate_import}
              onClick={() => commit(false)}>确认导入并校核</button>
            <button className="btn secondary" onClick={() => setPreview(null)}>取消</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- 校核工作台 ----------------

function Workbench({ showToast }: { showToast: (m: string, e?: boolean) => void }) {
  const [series, setSeries] = useState<StationSeries[]>([]);
  const [station, setStation] = useState("");
  const [metric, setMetric] = useState<"water_level" | "discharge">("water_level");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [statusFilter, setStatusFilter] = useState("open");
  const [issues, setIssues] = useState<Issue[]>([]);
  const [records, setRecords] = useState<HydroRecord[]>([]);
  const [selected, setSelected] = useState<Issue | null>(null);
  const [focusTime, setFocusTime] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    api.stations().then((s) => {
      setSeries(s);
      if (s.length && !station) {
        setStation(s[0].station_id);
      }
    });
  }, []);

  const stations = useMemo(
    () => [...new Set(series.map((s) => s.station_id))].sort(), [series]);
  const metrics = useMemo(
    () => series.filter((s) => s.station_id === station).map((s) => s.metric),
    [series, station]);

  const reload = useCallback(async () => {
    if (!station) return;
    const params: Record<string, string> = { station_id: station, metric };
    if (statusFilter) params.status_filter = statusFilter;
    if (start) params.start = start + "T00:00:00";
    if (end) params.end = end + "T23:59:59";
    const [iss, rec] = await Promise.all([
      api.issues(params),
      api.records(station, metric, start ? start + "T00:00:00" : undefined,
        end ? end + "T23:59:59" : undefined),
    ]);
    setIssues(iss);
    setRecords(rec.records);
    setSelected(null);
    setFocusTime(null);
  }, [station, metric, start, end, statusFilter]);

  useEffect(() => { reload(); }, [reload, reloadKey]);

  const afterAction = (msg: string) => {
    showToast(msg);
    setReloadKey((k) => k + 1);
  };

  const selectIssue = (i: Issue) => {
    setSelected(i);
    setFocusTime(i.gap_start || i.record_time);
  };

  return (
    <>
      <div className="card">
        <div className="filter-row">
          <label className="field">站点
            <select value={station} onChange={(e) => setStation(e.target.value)}>
              {stations.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label className="field">指标
            <select value={metric} onChange={(e) => setMetric(e.target.value as "water_level" | "discharge")}>
              <option value="water_level">水位</option>
              <option value="discharge">流量</option>
            </select>
          </label>
          <label className="field">开始日期
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="field">结束日期
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <label className="field">问题状态
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="open">待处理</option>
              <option value="resolved">已解决</option>
              <option value="ignored">已忽略（保留原值）</option>
              <option value="">全部</option>
            </select>
          </label>
          <button className="btn secondary" onClick={() => setReloadKey((k) => k + 1)}>
            重新校核 / 刷新</button>
        </div>
        {!metrics.includes(metric) && stations.length > 0 && (
          <div className="warning-box" style={{ marginBottom: 0 }}>
            该站点没有「{METRIC_LABEL[metric]}」数据，请切换指标或站点。
          </div>
        )}
      </div>

      <div className="workbench">
        <div className="card" style={{ marginBottom: 0 }}>
          <h2>问题列表（{issues.length}）</h2>
          <div className="issue-list">
            {issues.map((i) => (
              <div key={i.id}
                className={`issue-item ${selected?.id === i.id ? "selected" : ""} ${i.status}`}
                style={{ borderLeftColor: RULE_COLORS[i.rule_code] }}
                onClick={() => selectIssue(i)}>
                <div className="issue-head">
                  <span className="rule-tag" style={{ background: RULE_COLORS[i.rule_code] }}>
                    {i.rule_label}
                  </span>
                  <span className={`status-tag ${i.status}`}>
                    {i.status === "open" ? "待处理" : i.status === "resolved" ? "已解决" : "已忽略"}
                  </span>
                </div>
                <div className="issue-detail">{i.detail}</div>
                <div className="issue-meta">
                  {i.gap_start ? `${i.gap_start} ~ ${i.gap_end}` : i.record_time}
                  {i.effective_value !== null && i.rule_code !== "missing"
                    && ` · 当前值 ${i.effective_value}`}
                </div>
              </div>
            ))}
            {issues.length === 0 && <div style={{ color: "var(--text-muted)" }}>
              当前筛选条件下没有问题 🎉</div>}
          </div>
        </div>

        <div>
          <div className="card">
            <h2>{station} · {METRIC_LABEL[metric]} 过程线
              <span style={{ fontWeight: 400, fontSize: 12, color: "var(--text-muted)", marginLeft: 8 }}>
                共 {records.length} 条记录</span></h2>
            <TimeChart records={records} issues={issues} focusTime={focusTime}
              unit={records.find((r) => r.unit)?.unit || null}
              onFocus={setFocusTime} />
          </div>

          {selected && (
            <IssueActionPanel issue={selected} onDone={afterAction}
              onError={(m) => showToast(m, true)} />
          )}

          <div className="card">
            <h2>记录明细{focusTime && "（已定位到选中时间）"}</h2>
            <div style={{ maxHeight: 320, overflowY: "auto" }}>
              <table className="records">
                <thead><tr><th>监测时间</th><th>原始值</th><th>生效值</th><th>状态</th>
                  <th>质量标记</th><th>命中规则</th></tr></thead>
                <tbody>
                  {records.map((r) => (
                    <tr key={r.id}
                      className={`${focusTime === r.obs_time ? "highlight" : ""}
                        ${r.effective_status === "invalid" ? "invalid" : ""}
                        ${r.effective_status === "interpolated" ? "interpolated" : ""}`}>
                      <td>{r.obs_time.replace("T", " ")}</td>
                      <td>{r.raw_value === null ? "—" : r.raw_value}</td>
                      <td>{r.effective_value === null ? "—" : r.effective_value}</td>
                      <td>
                        {r.effective_status === "valid" && "有效"}
                        {r.effective_status === "invalid" &&
                          <span className="badge invalid">无效</span>}
                        {r.effective_status === "interpolated" &&
                          <span className="badge interp">插值</span>}
                        {r.raw_value !== null && r.effective_value !== r.raw_value
                          && r.effective_status === "valid" &&
                          <span className="badge edited">已修订</span>}
                      </td>
                      <td>{r.quality_flag || "—"}</td>
                      <td>{r.issue_codes || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

// ---------------- 问题处理面板 ----------------

function IssueActionPanel({ issue, onDone, onError }: {
  issue: Issue;
  onDone: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [newValue, setNewValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [interp, setInterp] = useState<null | Awaited<ReturnType<typeof api.interpSuggestion>>>(null);

  useEffect(() => {
    setReason(""); setNewValue(""); setInterp(null);
  }, [issue.id]);

  const doAction = async (action: string, extra?: Record<string, unknown>) => {
    if (!reason.trim()) { onError("请先填写处理原因"); return; }
    setBusy(true);
    try {
      await api.action({
        record_id: issue.record_id, action, reason,
        issue_id: issue.id, ...extra,
      });
      onDone(`已${{ keep: "保留原值并记录原因", edit: "修改数值", invalidate: "标记无效" }[action]}`);
    } catch (e) { onError((e as Error).message); }
    finally { setBusy(false); }
  };

  const loadSuggestion = async () => {
    try { setInterp(await api.interpSuggestion(issue.id)); }
    catch (e) { onError((e as Error).message); }
  };

  const doInterpolate = async () => {
    if (!reason.trim()) { onError("请填写插值确认原因"); return; }
    setBusy(true);
    try {
      await api.interpolate(issue.id, reason);
      onDone("已按线性插值补点（标记为插值，非原始观测），缺测问题已解决");
    } catch (e) { onError((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <div className="card">
      <h2>处理：
        <span className="rule-tag" style={{ background: RULE_COLORS[issue.rule_code], marginLeft: 6 }}>
          {issue.rule_label}</span>
        <span className={`status-tag ${issue.status}`} style={{ marginLeft: 8 }}>
          {issue.status === "open" ? "待处理" : issue.status === "resolved" ? "已解决" : "已忽略"}
        </span>
      </h2>
      <div className="issue-detail" style={{ fontSize: 13.5 }}>{issue.detail}</div>

      <div className="action-panel">
        <label className="field">处理原因（必填，会随修订数据导出、留痕可撤销）
          <textarea value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="例如：现场确认该时刻泄洪，水位突涨属实 / 经核对雨量记录修正为 12.42" />
        </label>

        {issue.rule_code === "missing" ? (
          <div>
            {!interp && (
              <div className="btns">
                <button className="btn secondary" onClick={loadSuggestion}>
                  查看线性插值建议</button>
                {issue.record_id && (
                  <button className="btn secondary" disabled={busy}
                    onClick={() => doAction("keep")}>
                    保留缺口（不补值，仅记录原因）</button>
                )}
              </div>
            )}
            {interp && !interp.eligible && (
              <div className="warning-box">不提供插值：{interp.reason}
                <div style={{ marginTop: 6 }}>
                  <button className="btn secondary small" onClick={() => setInterp(null)}>返回</button>
                </div>
              </div>
            )}
            {interp?.eligible && (
              <div className="info-box">
                <b>{interp.reason}</b>
                <table className="records" style={{ marginTop: 6 }}>
                  <thead><tr><th>补点时间</th><th>建议数值</th></tr></thead>
                  <tbody>
                    {interp.points!.map((p) => (
                      <tr key={p.obs_time}><td>{p.obs_time.replace("T", " ")}</td>
                        <td>{p.value}</td></tr>
                    ))}
                  </tbody>
                </table>
                <div className="btns">
                  <button className="btn" disabled={busy} onClick={doInterpolate}>
                    确认应用插值（人工补值，将明确标记）</button>
                  <button className="btn secondary" onClick={() => setInterp(null)}>取消</button>
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="btns">
            <button className="btn secondary" disabled={busy}
              onClick={() => doAction("keep")}>保留原值（确认无误）</button>
            <button className="btn" disabled={busy || !newValue.trim()}
              onClick={() => doAction("edit", { new_value: Number(newValue) })}>
              手动改值</button>
            <input type="number" step="0.01" placeholder="新数值"
              value={newValue} onChange={(e) => setNewValue(e.target.value)}
              style={{ width: 110 }} />
            <button className="btn danger" disabled={busy}
              onClick={() => doAction("invalidate")}>标记无效</button>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------- 规则配置 ----------------

function ConfigPage({ showToast }: { showToast: (m: string, e?: boolean) => void }) {
  const [series, setSeries] = useState<StationSeries[]>([]);
  const [configs, setConfigs] = useState<RuleConfig[]>([]);
  const [station, setStation] = useState("");
  const [metric, setMetric] = useState("water_level");
  const [form, setForm] = useState<Partial<RuleConfig>>({});

  useEffect(() => {
    api.stations().then((s) => {
      setSeries(s);
      if (s.length) setStation(s[0].station_id);
    });
    api.configs().then(setConfigs);
  }, []);

  const stations = [...new Set(series.map((s) => s.station_id))].sort();

  useEffect(() => {
    const existing = configs.find((c) => c.station_id === station && c.metric === metric);
    setForm(existing || {
      station_id: station, metric,
      expected_interval_minutes: 60, missing_gap_multiplier: 2, max_gap_intervals: 6,
      value_min: metric === "water_level" ? 0 : 0, value_max: metric === "water_level" ? 50 : 500,
      max_change_per_interval: metric === "water_level" ? 0.5 : 20,
      flat_run_minutes: 180,
    });
  }, [station, metric, configs]);

  const set = (k: keyof RuleConfig, v: string) =>
    setForm((f) => ({ ...f, [k]: v === "" ? null : Number(v) }));

  const save = async () => {
    try {
      await api.saveConfig({ ...(form as RuleConfig), station_id: station, metric });
      showToast("配置已保存，并已按新阈值重新校核");
      setConfigs(await api.configs());
    } catch (e) { showToast((e as Error).message, true); }
  };

  return (
    <div className="card">
      <h2>校核规则配置（按站点 + 指标）</h2>
      <div className="filter-row" style={{ marginBottom: 16 }}>
        <label className="field">站点
          <select value={station} onChange={(e) => setStation(e.target.value)}>
            {stations.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label className="field">指标
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            <option value="water_level">水位</option>
            <option value="discharge">流量</option>
          </select>
        </label>
      </div>
      <div className="config-grid">
        <label className="field">标称采样间隔（分钟）
          <input type="number" value={form.expected_interval_minutes ?? ""}
            onChange={(e) => set("expected_interval_minutes", e.target.value)} /></label>
        <label className="field">缺测判定倍数（间隔 × 倍数）
          <input type="number" step="0.5" value={form.missing_gap_multiplier ?? ""}
            onChange={(e) => set("missing_gap_multiplier", e.target.value)} /></label>
        <label className="field">允许插值最大缺口（间隔个数）
          <input type="number" value={form.max_gap_intervals ?? ""}
            onChange={(e) => set("max_gap_intervals", e.target.value)} /></label>
        <label className="field">数值合理下限
          <input type="number" step="0.1" value={form.value_min ?? ""}
            onChange={(e) => set("value_min", e.target.value)} /></label>
        <label className="field">数值合理上限
          <input type="number" step="0.1" value={form.value_max ?? ""}
            onChange={(e) => set("value_max", e.target.value)} /></label>
        <label className="field">每采样间隔最大变化量
          <input type="number" step="0.1" value={form.max_change_per_interval ?? ""}
            onChange={(e) => set("max_change_per_interval", e.target.value)} /></label>
        <label className="field">连续不变判定时长（分钟）
          <input type="number" value={form.flat_run_minutes ?? ""}
            onChange={(e) => set("flat_run_minutes", e.target.value)} /></label>
      </div>
      <div style={{ marginTop: 14 }}>
        <button className="btn" onClick={save}>保存并重新校核</button>
      </div>
      <p style={{ color: "var(--text-muted)", fontSize: 12.5, marginTop: 14 }}>
        未配置的站点+指标使用系统默认值（水位 0–50m、每小时变化 ≤0.5m；流量 0–500m³/s、
        每小时变化 ≤20m³/s；间隔 60 分钟、缺测 2 倍、插值上限 6 个间隔、持平 180 分钟）。
        样例中 ST03 为 30 分钟采样，已预置站点级配置。
      </p>
    </div>
  );
}

// ---------------- 处理记录 / 撤销 / 导出 ----------------

function HistoryPage({ showToast }: { showToast: (m: string, e?: boolean) => void }) {
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [series, setSeries] = useState<StationSeries[]>([]);
  const [station, setStation] = useState("");
  const [version, setVersion] = useState<"revised" | "raw">("revised");

  const reload = useCallback(async () => {
    setAudit(await api.audit());
    setSeries(await api.stations());
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const stations = [...new Set(series.map((s) => s.station_id))].sort();
  useEffect(() => { if (!station && stations.length) setStation(stations[0]); },
    [stations, station]);

  // 按批次聚合（插值批次含多条）
  const batches = useMemo(() => {
    const m = new Map<string, AuditEntry[]>();
    audit.forEach((a) => {
      const arr = m.get(a.batch_id) || [];
      arr.push(a); m.set(a.batch_id, arr);
    });
    return [...m.entries()].map(([batch_id, entries]) => ({
      batch_id, entries, first: entries[0],
    }));
  }, [audit]);

  const undo = async (batchId: string) => {
    if (!confirm("确定撤销该处理？记录将恢复到处理前状态，并自动重新校核。")) return;
    try {
      await api.undo(batchId);
      showToast("已撤销，相关校核结果已重新计算");
      reload();
    } catch (e) { showToast((e as Error).message, true); }
  };

  const ACTION_LABEL: Record<string, string> = {
    keep: "保留原值", edit: "手动改值", invalidate: "标记无效", interpolate: "线性插值补点",
  };

  return (
    <>
      <div className="card">
        <h2>数据导出</h2>
        <div className="filter-row">
          <label className="field">站点
            <select value={station} onChange={(e) => setStation(e.target.value)}>
              {stations.map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label className="field">导出内容
            <select value={version} onChange={(e) => setVersion(e.target.value as "revised" | "raw")}>
              <option value="revised">修订数据（含质量标记与处理原因）</option>
              <option value="raw">原始数据（不含人工补值）</option>
            </select>
          </label>
          <a className="btn" style={{ textDecoration: "none" }}
            href={api.exportUrl({ station_id: station, version })}>
            下载 CSV（{version === "raw" ? "原始" : "修订"}）</a>
        </div>
        <p style={{ color: "var(--text-muted)", fontSize: 12.5, marginTop: 12 }}>
          修订数据中：人工插值补点标记为「插值(人工补值,非原始观测)」、修改值标记为「人工修订」、
          无效记录数值留空并标记「无效」、经人工确认保留原值的标记「保留原值(人工确认)」。
          原始数据导出不包含任何人工补出来的点。
        </p>
      </div>

      <div className="card">
        <h2>处理记录（{batches.length} 个批次，刷新页面后仍保留，可撤销）</h2>
        <table className="records audit-table">
          <thead><tr><th>时间</th><th>站点/指标</th><th>记录时间</th><th>动作</th>
            <th>原值 → 新值</th><th>原因</th><th>操作</th></tr></thead>
          <tbody>
            {batches.flatMap((b) => b.entries.map((a) => (
              <tr key={a.id} className={a.undone ? "invalid" : ""}>
                <td>{a.created_at}</td>
                <td>{a.station_id} · {METRIC_LABEL[a.metric]}</td>
                <td>{a.obs_time.replace("T", " ")}</td>
                <td>{ACTION_LABEL[a.action]}
                  {b.entries.length > 1 && <span className="badge interp">
                    批次×{b.entries.length}</span>}</td>
                <td>{a.old_value ?? "—"} → {a.new_value ?? "（无效）"}</td>
                <td style={{ whiteSpace: "normal", maxWidth: 260 }}>{a.reason}</td>
                <td>
                  {a.undone ? <span className="undone">已撤销</span>
                    : <button className="btn danger small" onClick={() => undo(a.batch_id)}>
                      撤销{`${b.entries.length > 1 ? `整批(${b.entries.length})` : ""}`}</button>}
                </td>
              </tr>
            )))}
          </tbody>
        </table>
        {batches.length === 0 && <div style={{ color: "var(--text-muted)" }}>还没有处理记录。</div>}
      </div>
    </>
  );
}
