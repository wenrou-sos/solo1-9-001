import { useMemo, useRef, useState } from "react";
import type { HydroRecord, Issue } from "./types";
import { RULE_COLORS } from "./types";

interface Props {
  records: HydroRecord[];
  issues: Issue[];
  focusTime: string | null;
  unit: string | null;
  onFocus: (time: string | null) => void;
}

const W = 900;
const H = 340;
const PAD = { top: 20, right: 20, bottom: 34, left: 52 };

/** 时序曲线图：正常值实线、插值点青色虚线、无效值红叉；问题点按规则着色；
 * 缺口（相邻点时间间隔 > 1.5 个中位间隔）断线，不连线。 */
export default function TimeChart({ records, issues, focusTime, unit, onFocus }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<{ x: number; rec: HydroRecord } | null>(null);

  const { pts, xScale, yScale, medianStep, yTicks, xTicks } = useMemo(() => {
    const valid = records.filter(
      (r) => r.effective_value !== null && r.effective_status !== "invalid");
    const times = records.map((r) => new Date(r.obs_time).getTime());
    const t0 = Math.min(...times);
    const t1 = Math.max(...times);
    const vals = valid.map((r) => r.effective_value as number);
    const vmin = vals.length ? Math.min(...vals) : 0;
    const vmax = vals.length ? Math.max(...vals) : 1;
    const padV = (vmax - vmin) * 0.15 || 1;

    const xScale = (t: number) =>
      PAD.left + (t1 === t0 ? 0.5 : (t - t0) / (t1 - t0)) * (W - PAD.left - PAD.right);
    const yScale = (v: number) =>
      PAD.top + (1 - (v - (vmin - padV)) / ((vmax + padV) - (vmin - padV))) *
      (H - PAD.top - PAD.bottom);

    const steps = [];
    for (let i = 1; i < times.length; i++) steps.push(times[i] - times[i - 1]);
    steps.sort((a, b) => a - b);
    const medianStep = steps.length ? steps[Math.floor(steps.length / 2)] : 3600_000;

    const pts = records.map((r) => ({
      r,
      x: xScale(new Date(r.obs_time).getTime()),
      y: r.effective_value !== null && r.effective_status !== "invalid"
        ? yScale(r.effective_value) : null,
    }));

    const yTicks = Array.from({ length: 5 }, (_, i) => {
      const v = (vmin - padV) + (i / 4) * ((vmax + padV) - (vmin - padV));
      return { v, y: yScale(v) };
    });
    const xTicks = Array.from({ length: 6 }, (_, i) => {
      const t = t0 + (i / 5) * (t1 - t0);
      return { t, x: xScale(t), label: new Date(t).toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) };
    });
    return { pts, xScale, yScale, medianStep, yTicks, xTicks };
  }, [records]);

  // 分段连线：缺口或无效点处断开；插值段用虚线
  const segments = useMemo(() => {
    const segs: { d: string; interp: boolean }[] = [];
    let cur: HydroRecord[] = [];
    const flush = () => {
      if (cur.length >= 2) {
        const d = cur.map((r, i) => {
          const p = pts.find((q) => q.r.id === r.id)!;
          return `${i === 0 ? "M" : "L"}${p.x},${p.y}`;
        }).join("");
        segs.push({
          d,
          interp: cur.some((r) => r.effective_status === "interpolated"),
        });
      }
      cur = [];
    };
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      if (p.y === null) { flush(); continue; }
      if (cur.length) {
        const prev = pts[i - 1];
        const gap = new Date(p.r.obs_time).getTime() -
          new Date(prev.r.obs_time).getTime();
        if (gap > medianStep * 1.5) flush();
      }
      cur.push(p.r);
    }
    flush();
    return segs;
  }, [pts, medianStep]);

  const issueByRecord = useMemo(() => {
    const m = new Map<number, Issue[]>();
    issues.forEach((i) => {
      if (i.record_id == null) return;
      const arr = m.get(i.record_id) || [];
      arr.push(i);
      m.set(i.record_id, arr);
    });
    return m;
  }, [issues]);

  // 缺测缺口高亮带
  const gapBands = issues
    .filter((i) => i.rule_code === "missing" && i.gap_start && i.gap_end)
    .map((i) => ({
      id: i.id,
      x1: xScale(new Date(i.gap_start as string).getTime()),
      x2: xScale(new Date(i.gap_end as string).getTime()),
    }));

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current!.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * W;
    let best: { x: number; rec: HydroRecord } | null = null;
    for (const p of pts) {
      if (Math.abs(p.x - mx) < 12 && (!best || Math.abs(p.x - mx) < Math.abs(best.x - mx))) {
        best = { x: p.x, rec: p.r };
      }
    }
    setHover(best);
  };

  return (
    <div className="chart-wrap">
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", minWidth: 640 }}
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        onClick={() => onFocus(hover ? hover.rec.obs_time : null)}>
        {/* 网格与坐标轴 */}
        {yTicks.map((t) => (
          <g key={t.y}>
            <line x1={PAD.left} x2={W - PAD.right} y1={t.y} y2={t.y}
              stroke="#e8e7e2" strokeWidth={1} />
            <text x={PAD.left - 8} y={t.y + 4} textAnchor="end" fontSize={11}
              fill="#85837c">{t.v.toFixed(1)}</text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={t.x} x={t.x} y={H - 10} textAnchor="middle" fontSize={11}
            fill="#85837c">{t.label}</text>
        ))}
        <text x={14} y={16} fontSize={11} fill="#85837c">{unit || ""}</text>

        {/* 缺测缺口带 */}
        {gapBands.map((g) => (
          <rect key={g.id} x={g.x1} y={PAD.top} width={Math.max(g.x2 - g.x1, 2)}
            height={H - PAD.top - PAD.bottom} fill="#0f9b8e" opacity={0.08} />
        ))}

        {/* 选中定位线 */}
        {focusTime && (
          <line x1={xScale(new Date(focusTime).getTime())}
            x2={xScale(new Date(focusTime).getTime())}
            y1={PAD.top} y2={H - PAD.bottom} stroke="#d03b3b" strokeWidth={1.5}
            strokeDasharray="4 3" />
        )}

        {/* 曲线 */}
        {segments.map((s, i) => (
          <path key={i} d={s.d} fill="none"
            stroke={s.interp ? "#0f9b8e" : "#2a78d6"}
            strokeWidth={2} strokeDasharray={s.interp ? "5 4" : undefined}
            strokeLinejoin="round" />
        ))}

        {/* 数据点 + 问题标记 */}
        {pts.map((p) => {
          if (p.y === null) {
            return (
              <g key={p.r.id}>
                <line x1={p.x - 5} y1={H / 2 - 5} x2={p.x + 5} y2={H / 2 + 5}
                  stroke="#d03b3b" strokeWidth={2} />
                <line x1={p.x - 5} y1={H / 2 + 5} x2={p.x + 5} y2={H / 2 - 5}
                  stroke="#d03b3b" strokeWidth={2} />
              </g>
            );
          }
          const ii = issueByRecord.get(p.r.id) || [];
          const color = ii.length ? RULE_COLORS[ii[0].rule_code] : "#2a78d6";
          return (
            <circle key={p.r.id} cx={p.x} cy={p.y}
              r={ii.length ? 5.5 : 3}
              fill={p.r.effective_status === "interpolated" ? "#0f9b8e" : "#fcfcfb"}
              stroke={color} strokeWidth={ii.length ? 2.5 : 1.5} />
          );
        })}

        {/* hover 十字线与提示 */}
        {hover && (
          <g>
            <line x1={hover.x} x2={hover.x} y1={PAD.top} y2={H - PAD.bottom}
              stroke="#52514e" strokeWidth={1} strokeDasharray="3 3" />
            <g transform={`translate(${Math.min(hover.x + 10, W - 210)}, ${PAD.top + 6})`}>
              <rect width={200} height={48} rx={6} fill="#2a2a27" opacity={0.92} />
              <text x={10} y={19} fontSize={11.5} fill="#fff">
                {new Date(hover.rec.obs_time).toLocaleString("zh-CN")}
              </text>
              <text x={10} y={37} fontSize={12} fill="#ffd9a8">
                {hover.rec.effective_status === "invalid"
                  ? "无效（无生效值）"
                  : `值：${hover.rec.effective_value}${hover.rec.unit || ""}`
                  + (hover.rec.raw_value === null ? "（插值）" : "")}
              </text>
            </g>
          </g>
        )}
      </svg>
      <div className="legend">
        <span className="item"><span className="swatch" style={{ background: "#2a78d6" }} />
          有效观测</span>
        <span className="item"><span className="swatch" style={{ background: "#0f9b8e" }} />
          插值补点 / 缺测缺口</span>
        <span className="item"><span className="swatch" style={{ background: "#d03b3b" }} />
          ✕ 无效点 / 问题标记</span>
        <span className="item" style={{ marginLeft: "auto" }}>
          问题点按规则颜色描边；点击图表定位到悬停时刻</span>
      </div>
    </div>
  );
}
