#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-leverage —— 测测你是不是在当 AI 的保姆。

读取本地 ZCode 会话数据库（~/.zcode/cli/db/db.sqlite），统计你的真实
人机杠杆比：你敲了多少条指令，agent 们执行了多少个动作。全程本地计算，
不上传任何数据。

用法：
    python3 agent_leverage.py                  # 默认最近 30 天，终端报告
    python3 agent_leverage.py --days 14
    python3 agent_leverage.py --html card.html # 生成可截图分享的数据卡
    python3 agent_leverage.py --json out.json --md out.md
    python3 agent_leverage.py --db /path/to/db.sqlite --from 2026-08-25 --to 2026-09-24

口径与《从执行者到指挥官》分享一致：
- 用户输入：interactive 会话内 role=user 的消息（fork/subagent 里的
  user 消息是系统注入或 agent 生成，不计入人类输入）
- agent 动作：全部会话的 tool_usage 记录
- 杠杆比   = agent 动作 / 用户输入
- 夜间     = 本地时间 22:00–07:00 的 token 占比（computed_total_tokens）
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DEFAULT_DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")

# ---------------------------------------------------------------- 判定文案

NANNY_TIERS = [
    (5,   "人肉保姆", "🧑‍🍼", "AI 坐在旁边看你干活。它不是不会，是你不让。"),
    (10,  "贴身管家", "🧹",   "偶尔舍得放手了，但每一步都要盯——累的是你。"),
    (20,  "包工头",   "🦺",   "你已经会让 AI 干活了，但还在现场监工。"),
    (50,  "指挥官",   "🎖️",   "一条指令 ≈ {ratio} 个动作。产出物不再是代码，是任务定义和验收标准。"),
    (1 << 30, "影子司令官", "👻", "你睡觉的时候它自转。团队里传说中'背后站着 agent'的那个人就是你。"),
]

NIGHT_TIERS = [
    (10, "纯日班", "你的 agent 准时下班。8 小时工作制的天花板还没破。"),
    (25, "开始加班", "夜里已经有动静了，但主力还是你的白天。"),
    (101, "时间平移达成", "约 1/3 的工作量发生在你睡觉时。早晨验收，晚上布置——这才是完整的夜循环。"),
]


def nanny_verdict(ratio):
    for upper, name, emoji, line in NANNY_TIERS:
        if ratio < upper:
            return name, emoji, line.format(ratio=ratio)
    return NANNY_TIERS[-1][1:]


def night_verdict(pct):
    for upper, name, line in NIGHT_TIERS:
        if pct < upper:
            return name, line
    return NIGHT_TIERS[-1][1], NIGHT_TIERS[-1][2]


# ---------------------------------------------------------------- 数据采集

def window_bounds(args):
    """返回 (start_ms, end_ms, 起止日期字符串)。"""
    now = datetime.now()
    if args.from_date or args.to_date:
        end = datetime.strptime(args.to_date, "%Y-%m-%d") if args.to_date else now
        start = datetime.strptime(args.from_date, "%Y-%m-%d") if args.from_date \
            else end - timedelta(days=args.days)
    else:
        end = now
        start = now - timedelta(days=args.days)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), start, end


def collect(conn, start_ms, end_ms, start_dt, end_dt):
    m = {"start": start_ms, "end": end_ms}

    def one(sql):
        return conn.execute(sql, m).fetchone()[0]

    # 会话（按创建时间落入窗口）
    row = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN task_type='interactive' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN task_type IN ('subagent_child','fork') THEN 1 ELSE 0 END)
           FROM session WHERE time_created >= :start AND time_created < :end""",
        m).fetchone()
    stats = {
        "sessions_total": row[0] or 0,
        "sessions_interactive": row[1] or 0,
        "sessions_subagent": row[2] or 0,
    }

    # agent 动作：全部会话的工具调用（事件时间落窗口）
    stats["tool_calls"] = one(
        "SELECT COUNT(*) FROM tool_usage WHERE started_at >= :start AND started_at < :end")

    # 用户输入：interactive 会话里的 role=user 消息
    stats["user_inputs"] = conn.execute(
        """SELECT COUNT(*) FROM message
           WHERE time_created >= :start AND time_created < :end
             AND json_extract(data,'$.role')='user'
             AND session_id IN (SELECT id FROM session WHERE task_type='interactive')""",
        m).fetchone()[0]
    stats["assistant_msgs"] = conn.execute(
        """SELECT COUNT(*) FROM message
           WHERE time_created >= :start AND time_created < :end
             AND json_extract(data,'$.role')='assistant'""",
        m).fetchone()[0]

    # token 总量 + 夜间占比（本地时区 22:00–07:00）
    row = conn.execute(
        """SELECT COALESCE(SUM(computed_total_tokens),0),
                  COALESCE(SUM(CASE WHEN strftime('%H', started_at/1000,'unixepoch','localtime') >= '22'
                                     OR strftime('%H', started_at/1000,'unixepoch','localtime') < '07'
                                THEN computed_total_tokens ELSE 0 END),0)
           FROM model_usage WHERE started_at >= :start AND started_at < :end""", m).fetchone()
    stats["tokens_total"] = row[0]
    stats["tokens_night"] = row[1]

    # 活跃天数 / 峰值日 / 每日 token 曲线
    daily = conn.execute(
        """SELECT date(started_at/1000,'unixepoch','localtime') d,
                  SUM(computed_total_tokens)
           FROM model_usage WHERE started_at >= :start AND started_at < :end
           GROUP BY d ORDER BY d""", m).fetchall()
    stats["active_days"] = len(daily)
    stats["window_days"] = max(1, (end_dt.date() - start_dt.date()).days)
    stats["peak_day"] = max(daily, key=lambda r: r[1]) if daily else (None, 0)
    stats["daily_tokens"] = daily

    # 模型分层（token 前 5）
    stats["models"] = conn.execute(
        """SELECT model_id, SUM(computed_total_tokens) t
           FROM model_usage WHERE started_at >= :start AND started_at < :end
           GROUP BY model_id ORDER BY t DESC LIMIT 5""", m).fetchall()

    # 数据库实际覆盖范围（用于提示数据不足）
    row = conn.execute("SELECT MIN(started_at), MAX(started_at) FROM model_usage").fetchone()
    stats["db_min"], stats["db_max"] = row
    return stats


# ---------------------------------------------------------------- 报告渲染

def fmt_num(n):
    if n is None:
        return "0"
    if n >= 1e8:
        return "%.2f 亿" % (n / 1e8)
    return f"{n:,.0f}"


def derive(stats):
    inputs = max(1, stats["user_inputs"])
    d = {
        "ratio": stats["tool_calls"] / inputs,
        "amplification": stats["assistant_msgs"] / inputs,
        "night_pct": 100.0 * stats["tokens_night"] / stats["tokens_total"] if stats["tokens_total"] else 0.0,
        "subagent_pct": 100.0 * stats["sessions_subagent"] / stats["sessions_total"] if stats["sessions_total"] else 0.0,
        "inputs_per_day": stats["user_inputs"] / stats["window_days"],
        "tokens_per_day": stats["tokens_total"] / stats["window_days"],
    }
    d["nanny"] = nanny_verdict(d["ratio"])
    d["night"] = night_verdict(d["night_pct"])
    return d


def render_terminal(stats, d, window_label, use_color=True):
    C = lambda code: (lambda s: f"\033[{code}m{s}\033[0m") if use_color else (lambda s: s)
    name, emoji, line = d["nanny"]
    nname, nline = d["night"]
    L = []
    L.append("")
    L.append(C("1;36")("  agent-leverage · 测测你是不是在当 AI 的保姆"))
    L.append(C("2")(f"  窗口：{window_label}"))
    L.append("  " + "─" * 46)
    L.append("")
    L.append(f"  你的指令       {C('1')(fmt_num(stats['user_inputs']))} 条")
    L.append(f"  agent 动作     {C('1')(fmt_num(stats['tool_calls']))} 次")
    L.append(f"  token 消耗     {C('1')(fmt_num(stats['tokens_total']))}")
    L.append(f"  会话           {stats['sessions_total']:,}（主会话 {stats['sessions_interactive']:,} + subagent {stats['sessions_subagent']:,}）")
    L.append(f"  活跃天数       {stats['active_days']} / {stats['window_days']} 天")
    L.append("")
    L.append(C("1;33")(f"  人机杠杆比     1 : {d['ratio']:.1f}"))
    L.append(C("1;33")(f"  夜间工作量     {d['night_pct']:.1f}%  [{nname}]"))
    L.append(C("1;33")(f"  subagent 占比  {d['subagent_pct']:.0f}%"))
    L.append("")
    L.append(C("1;35")(f"  保姆指数判定：{emoji} {name}"))
    L.append(f"  {line}")
    L.append("")
    if stats["models"]:
        top = stats["models"][0]
        L.append(C("2")("  模型分层 top" + str(len(stats["models"]))))
        for mid, tok in stats["models"]:
            pct = 100.0 * tok / stats["tokens_total"] if stats["tokens_total"] else 0
            bar = "█" * max(1, int(pct / 4))
            L.append(f"    {mid:<28} {pct:5.1f}%  {C('2')(bar)}")
        L.append("")
    pk = stats["peak_day"]
    if pk and pk[0]:
        L.append(C("2")(f"  峰值日 {pk[0]}：{fmt_num(pk[1])} tokens"))
        L.append("")
    return "\n".join(L)


def render_html(stats, d, window_label):
    name, emoji, line = d["nanny"]
    nname, _ = d["night"]

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 每日 token 迷你柱状图（纯 SVG）
    daily = stats["daily_tokens"]
    bars = ""
    if daily:
        peak = max(t for _, t in daily) or 1
        bw = 560.0 / len(daily)
        for i, (day, tok) in enumerate(daily):
            h = max(2, 56.0 * tok / peak)
            x = i * bw
            bars += (f'<rect x="{x:.1f}" y="{70-h:.1f}" width="{bw*0.72:.1f}" height="{h:.1f}" '
                     f'rx="1.5" fill="#38bdf8" opacity="0.85"/>')
    models_rows = "".join(
        f'<div class="mrow"><span>{esc(mid)}</span><b>{(100.0*tok/stats["tokens_total"] if stats["tokens_total"] else 0):.1f}%</b></div>'
        for mid, tok in stats["models"])

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>agent-leverage · {esc(name)}</title></head>
<body style="margin:0;background:#0b1220;display:flex;justify-content:center;padding:32px 12px;font-family:'PingFang SC','Microsoft YaHei',sans-serif">
<div style="width:620px;background:linear-gradient(160deg,#0d1526 0%,#101b33 60%,#0d2a45 100%);border:1px solid #1e3a5f;border-radius:20px;padding:36px 40px;color:#dbeafe">
  <div style="font-size:13px;letter-spacing:3px;color:#38bdf8">AGENT-LEVERAGE</div>
  <h1 style="margin:6px 0 2px;font-size:22px;color:#f8fafc">测测你是不是在当 AI 的保姆</h1>
  <div style="font-size:12px;color:#64748b">{esc(window_label)} · 全本地计算</div>

  <div style="display:flex;align-items:baseline;gap:14px;margin:26px 0 4px">
    <span style="font-size:64px;font-weight:800;color:#f8fafc;line-height:1">1:{d["ratio"]:.0f}</span>
    <span style="font-size:15px;color:#94a3b8">人机杠杆比</span>
  </div>
  <div style="font-size:15px;color:#fbbf24;margin-bottom:4px">{emoji} 判定：<b>{esc(name)}</b></div>
  <div style="font-size:13px;color:#94a3b8;line-height:1.7">{esc(line)}</div>

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:26px">
    <div class="cell"><b>{fmt_num(stats['user_inputs'])}</b><span>我的指令</span></div>
    <div class="cell"><b>{fmt_num(stats['tool_calls'])}</b><span>agent 动作</span></div>
    <div class="cell"><b>{fmt_num(stats['tokens_total'])}</b><span>tokens</span></div>
    <div class="cell"><b>{d['night_pct']:.1f}%</b><span>夜间工作量 · {esc(nname)}</span></div>
    <div class="cell"><b>{stats['sessions_total']:,}</b><span>会话（subagent {stats['sessions_subagent']:,}）</span></div>
    <div class="cell"><b>{stats['active_days']}/{stats['window_days']}</b><span>活跃天数</span></div>
  </div>

  <div style="margin-top:24px">
    <div style="font-size:12px;color:#64748b;margin-bottom:6px">每日 token 消耗</div>
    <svg width="100%" viewBox="0 0 560 74" preserveAspectRatio="none">{bars}</svg>
  </div>

  <div style="margin-top:18px">
    <div style="font-size:12px;color:#64748b;margin-bottom:6px">模型分层</div>
    {models_rows}
  </div>

  <div style="margin-top:26px;padding-top:14px;border-top:1px solid #1e3a5f;font-size:11px;color:#475569">
    agent-leverage · 你的每一条指令都有记录 · 数据永远不出本机
  </div>
</div>
<style>
.cell{{background:rgba(56,189,248,.06);border:1px solid #1e3a5f;border-radius:12px;padding:12px 14px}}
.cell b{{display:block;font-size:18px;color:#f8fafc}}
.cell span{{font-size:11px;color:#64748b}}
.mrow{{display:flex;justify-content:space-between;font-size:12px;color:#94a3b8;padding:3px 0}}
</style>
</body></html>"""


def render_md(stats, d, window_label):
    name, emoji, line = d["nanny"]
    nname, _ = d["night"]
    models = "\n".join(
        f"| {mid} | {tok:,} | {(100.0*tok/stats['tokens_total'] if stats['tokens_total'] else 0):.1f}% |"
        for mid, tok in stats["models"])
    return f"""# agent-leverage 报告 · {window_label}

**人机杠杆比：1 : {d['ratio']:.1f}** —— {emoji} 判定：**{name}**

> {line}

| 指标 | 数值 |
|---|---|
| 我的指令 | {stats['user_inputs']:,} 条（日均 {d['inputs_per_day']:.0f}） |
| agent 动作 | {stats['tool_calls']:,} 次 |
| 消息放大 | {d['amplification']:.1f} 条助手消息 / 条指令 |
| token 消耗 | {fmt_num(stats['tokens_total'])} |
| 夜间工作量（22:00–07:00） | {d['night_pct']:.1f}% · {nname} |
| 会话 | {stats['sessions_total']:,}（subagent 占 {d['subagent_pct']:.0f}%） |
| 活跃天数 | {stats['active_days']} / {stats['window_days']} |

## 模型分层

| 模型 | tokens | 占比 |
|---|---|---|
{models}

---
*口径：用户输入 = interactive 会话内 role=user 消息；agent 动作 = 全部会话工具调用；token = computed_total_tokens。数据不出本机。*
"""


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="测测你是不是在当 AI 的保姆（本地 ZCode 会话库分析）")
    ap.add_argument("--db", default=DEFAULT_DB, help="会话数据库路径（默认 ~/.zcode/cli/db/db.sqlite）")
    ap.add_argument("--days", type=int, default=30, help="统计窗口天数（默认 30）")
    ap.add_argument("--from", dest="from_date", help="窗口起点 YYYY-MM-DD")
    ap.add_argument("--to", dest="to_date", help="窗口终点 YYYY-MM-DD")
    ap.add_argument("--html", help="输出分享卡 HTML 到指定路径")
    ap.add_argument("--md", help="输出 Markdown 报告到指定路径")
    ap.add_argument("--json", dest="json_out", help="输出原始 JSON 到指定路径")
    ap.add_argument("--no-color", action="store_true", help="终端不带 ANSI 颜色")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"找不到会话数据库：{args.db}\n（用 --db 指定路径）")

    start_ms, end_ms, start_dt, end_dt = window_bounds(args)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        stats = collect(conn, start_ms, end_ms, start_dt, end_dt)
    finally:
        conn.close()

    # 数据覆盖不足时提醒（库一般只保留 ~30 天）
    if stats["db_min"] and stats["db_min"] > start_ms:
        have = datetime.fromtimestamp(stats["db_min"] / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"⚠ 数据库只保留到 {have} 开始的数据，以下为实际覆盖范围内的统计", file=sys.stderr)

    d = derive(stats)
    window_label = f"{start_dt:%Y-%m-%d} → {end_dt:%Y-%m-%d}（{args.days} 天）" \
        if not (args.from_date or args.to_date) else f"{start_dt:%Y-%m-%d} → {end_dt:%Y-%m-%d}"

    print(render_terminal(stats, d, window_label, use_color=not args.no_color))

    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(stats, d, window_label))
        print(f"✔ 分享卡已生成：{args.html}", file=sys.stderr)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(render_md(stats, d, window_label))
        print(f"✔ Markdown 报告已生成：{args.md}", file=sys.stderr)
    if args.json_out:
        payload = dict(stats)
        payload["derived"] = {k: v for k, v in d.items() if k not in ("nanny", "night")}
        payload["derived"]["nanny"] = {"name": d["nanny"][0], "emoji": d["nanny"][1], "line": d["nanny"][2]}
        payload["derived"]["night"] = {"name": d["night"][0], "line": d["night"][1]}
        payload["window_label"] = window_label
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"✔ JSON 已生成：{args.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
