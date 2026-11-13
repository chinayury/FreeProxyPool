# -*- coding: utf-8 -*-
"""每日质量快照（借鉴 docs/09 P2-2）。

借鉴 Thordata 的"每天产出一份可用清单"思路：给调用方一个
"**昨日可用代理清单**"的参考，而不只是实时 API。

**只借鉴这一项，不借鉴零服务器架构本身** —— Actions 单次运行有 6 小时
限制、无法提供实时 API 与管理后台，不满足本项目定位（见 docs/09 P2-2）。

产出（写到 ``--out-dir``）：

======================  ==================================================
文件                     内容
======================  ==================================================
``snapshot.json``       全量快照（含 tier / 匿名度 / 地域 / 延迟 / 协议）
``all.txt``             一行一个 ``ip:port``
``<CODE>.txt``          按国家/地区分文件（``CN.txt`` / ``US.txt`` …）
``index.html``          静态看板（浅色主题 + 深浅切换）
======================  ==================================================

数据源二选一：

* ``--base-url``：走 API（用 SDK，顺便验证 SDK 可用）；
* 默认：**直读 Redis**（在生产机上 cron 跑最省事，不需要 API Key）。

用法::

    python tools/daily_snapshot.py --out-dir snapshot
    python tools/daily_snapshot.py --base-url http://127.0.0.1:5020 \\
        --api-key "$API_KEY" --out-dir snapshot

**产物里不含任何凭据**：只写代理地址与公开属性。
"""
import argparse
import json
import os
import time
from collections import Counter


def collect_from_redis(table: str = None) -> list:
    """直读 Redis 取全池代理（生产机 cron 跑用这个，不需要 API Key）。"""
    from ..db.dbClient import get_store
    store = get_store()
    if table:
        store.change_table(table)
    return store.get_all()


def collect_from_api(base_url: str, api_key: str = None) -> list:
    """走 API 取全池代理（顺便验证 SDK 可用）。"""
    from ..sdk import ProxyPoolClient
    pool = ProxyPoolClient(base_url, api_key=api_key)
    try:
        return pool.all()
    finally:
        pool.close()


def build_rows(items) -> list:
    """把池里的原始记录整理成快照行。"""
    from ..helper.proxy import normalize_protocol, tier_of_proxy

    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        proxy = str(item.get("proxy") or "")
        if not proxy:
            continue
        try:
            speed = round(float(item.get("ewma_speed") or item.get("speed") or 0), 4)
        except (TypeError, ValueError):
            speed = 0.0
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        rows.append({
            "proxy": proxy,
            "protocol": normalize_protocol(item.get("protocol")),
            "https": bool(item.get("https")),
            "region": str(item.get("region") or "").strip().upper(),
            "anonymous": str(item.get("anonymous") or ""),
            "ip_type": str(item.get("ip_type") or "unknown"),
            "tier": tier_of_proxy(item),
            "speed": speed,
            "score": score,
            "source": str(item.get("source") or ""),
        })
    rows.sort(key=lambda r: (-r["score"], r["proxy"]))
    return rows


def write_snapshot(rows, out_dir: str, generated_at: str = None) -> dict:
    """写出全部产物，返回统计信息。"""
    import html

    stamp = generated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(out_dir, exist_ok=True)

    # 1) 全量 JSON
    with open(os.path.join(out_dir, "snapshot.json"), "w", encoding="utf-8") as fp:
        json.dump({"generated_at": stamp, "count": len(rows), "proxies": rows},
                  fp, ensure_ascii=False, indent=2)

    # 2) 全量纯文本
    with open(os.path.join(out_dir, "all.txt"), "w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(row["proxy"] + "\n")

    # 3) 按国家分文件
    by_region = {}
    for row in rows:
        by_region.setdefault(row["region"] or "UNKNOWN", []).append(row["proxy"])
    for code, proxies in by_region.items():
        with open(os.path.join(out_dir, "%s.txt" % code), "w",
                  encoding="utf-8") as fp:
            for proxy in proxies:
                fp.write(proxy + "\n")

    # 4) 静态看板
    stats = {
        "total": len(rows),
        "https": sum(1 for r in rows if r["https"]),
        "by_region": Counter(r["region"] or "UNKNOWN" for r in rows),
        "by_tier": Counter(r["tier"] for r in rows),
        "by_protocol": Counter(r["protocol"] for r in rows),
        "by_anonymous": Counter(r["anonymous"] or "unknown" for r in rows),
    }
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(render_index(stats, stamp))
    stats["by_region"] = dict(stats["by_region"])
    stats["by_tier"] = dict(stats["by_tier"])
    stats["by_protocol"] = dict(stats["by_protocol"])
    stats["by_anonymous"] = dict(stats["by_anonymous"])
    return stats


def render_index(stats: dict, stamp: str) -> str:
    """渲染静态看板（浅色主题 + 深浅切换开关）。"""
    import html

    def rows_of(counter: dict, total: int) -> str:
        if not counter:
            return "<tr><td colspan='2'>（无）</td></tr>"
        out = []
        for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
            pct = (count * 100.0 / total) if total else 0
            out.append(
                "<tr><td>%s</td><td class='num'>%d</td>"
                "<td><div class='bar'><i style='width:%.1f%%'></i></div>"
                "<span class='pct'>%.1f%%</span></td></tr>"
                % (html.escape(str(key)), count, pct, pct))
        return "\n".join(out)

    total = max(1, int(stats.get("total") or 0))
    return """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FreeProxyPool 每日快照</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2328;--muted:#656d76;--line:#e3e6ea;
      --accent:#2f6feb;--bar:#d7e3fb;--shadow:0 1px 3px rgba(16,22,26,.08)}
[data-theme="dark"]{--bg:#14171a;--card:#1c2024;--fg:#e6edf3;--muted:#9198a1;
      --line:#2b3138;--accent:#6ea8ff;--bar:#24334d;--shadow:none}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
     font:14px/1.6 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}
.wrap{max-width:960px;margin:0 auto}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:19px;margin:0;font-weight:650}
.meta{color:var(--muted);font-size:13px}
button{margin-left:auto;border:1px solid var(--line);background:var(--card);
       color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--accent)}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:14px 16px;box-shadow:var(--shadow)}
.card h2{font-size:13px;margin:0 0 10px;color:var(--muted);font-weight:600;
         text-transform:uppercase;letter-spacing:.04em}
table{width:100%%;border-collapse:collapse}
td{padding:3px 0;vertical-align:middle}
td.num{text-align:right;padding-right:10px;font-variant-numeric:tabular-nums}
.bar{display:inline-block;width:120px;height:7px;background:var(--bar);
     border-radius:4px;overflow:hidden;vertical-align:middle}
.bar i{display:block;height:100%%;background:var(--accent)}
.pct{color:var(--muted);font-size:12px;margin-left:8px}
.kpis{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;
     padding:12px 16px;min-width:120px;box-shadow:var(--shadow)}
.kpi b{display:block;font-size:22px;font-weight:650}
.kpi span{color:var(--muted);font-size:12px}
footer{margin-top:22px;color:var(--muted);font-size:12px}
a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>FreeProxyPool 每日质量快照</h1>
  <span class="meta">生成时间 %s</span>
  <button id="theme-toggle" type="button">切换深色</button>
</header>

<div class="kpis">
  <div class="kpi"><b>%d</b><span>代理总数</span></div>
  <div class="kpi"><b>%d</b><span>支持 HTTPS</span></div>
  <div class="kpi"><b>%d</b><span>国家/地区</span></div>
</div>

<div class="grid">
  <div class="card"><h2>按国家/地区</h2><table>%s</table></div>
  <div class="card"><h2>按延迟分层</h2><table>%s</table></div>
  <div class="card"><h2>按传输协议</h2><table>%s</table></div>
  <div class="card"><h2>按匿名度</h2><table>%s</table></div>
</div>

<footer>
  产物：<a href="snapshot.json">snapshot.json</a> ·
  <a href="all.txt">all.txt</a> · 按国家分文件 <code>&lt;CODE&gt;.txt</code><br>
  本页为静态快照，仅供调用方参考；实时可用性请以 API 为准。
  数据来自 FreeProxyPool v0.1.0。
</footer>
</div>
<script>
(function(){
  var KEY='fpp-snapshot-theme';
  var root=document.documentElement;
  var saved=null;
  try{saved=localStorage.getItem(KEY);}catch(e){}
  if(saved){root.setAttribute('data-theme',saved);}
  function label(){
    document.getElementById('theme-toggle').textContent=
      root.getAttribute('data-theme')==='dark'?'切换浅色':'切换深色';
  }
  label();
  document.getElementById('theme-toggle').addEventListener('click',function(){
    var next=root.getAttribute('data-theme')==='dark'?'light':'dark';
    root.setAttribute('data-theme',next);
    try{localStorage.setItem(KEY,next);}catch(e){}
    label();
  });
})();
</script>
</body>
</html>
""" % (html.escape(stamp), stats.get("total") or 0, stats.get("https") or 0,
       len(stats.get("by_region") or {}), rows_of(stats.get("by_region") or {}, total),
       rows_of(stats.get("by_tier") or {}, total),
       rows_of(stats.get("by_protocol") or {}, total),
       rows_of(stats.get("by_anonymous") or {}, total))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成每日质量快照（借鉴 docs/09 P2-2）")
    parser.add_argument("--base-url", default="",
                        help="从 API 取数（留空则直读 Redis）")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY", ""),
                        help="API Key（默认取环境变量 API_KEY）")
    parser.add_argument("--out-dir", default="snapshot",
                        help="产物目录（默认 snapshot/）")
    parser.add_argument("--table", default="",
                        help="自定义 Redis 表名（默认 use_proxy）")
    args = parser.parse_args()

    if args.base_url:
        items = collect_from_api(args.base_url, args.api_key or None)
    else:
        items = collect_from_redis(args.table or None)

    rows = build_rows(items)
    stats = write_snapshot(rows, args.out_dir)
    print("快照已生成：%s" % os.path.abspath(args.out_dir))
    print("  代理总数 %d，其中支持 HTTPS %d" % (stats["total"], stats["https"]))
    print("  国家/地区 %d 个；延迟分布 %s"
          % (len(stats["by_region"]), stats["by_tier"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
