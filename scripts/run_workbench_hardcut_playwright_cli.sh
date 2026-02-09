#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$E2E_ROOT/.." && pwd)"

if ! command -v npx >/dev/null 2>&1; then
  echo "[ERROR] npx is required. Install Node.js/npm first." >&2
  exit 1
fi

if [[ -f "$E2E_ROOT/.env" ]]; then
  # shellcheck disable=SC1090
  source "$E2E_ROOT/.env"
fi

export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export PWCLI="${PWCLI:-$CODEX_HOME/skills/playwright/scripts/playwright_cli.sh}"
if [[ ! -x "$PWCLI" ]]; then
  echo "[ERROR] Playwright CLI wrapper not found: $PWCLI" >&2
  exit 1
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_ROOT="${1:-$REPO_ROOT/output/playwright/workbench-hardcut/$TIMESTAMP}"
WORKBENCH_URL="${WORKBENCH_UI_URL:-${PW_WORKBENCH_URL:-http://localhost/workbench?assistant=1}}"
LAWYER_USER="${LAWYER_USERNAME:-lawyer1}"
LAWYER_PASS="${LAWYER_PASSWORD:-lawyer123456}"
FIRST_PROMPT="${WORKBENCH_FIRST_PROMPT:-请基于当前案件先完成事实、证据、候选案由梳理。}"
UPLOAD_PROMPT="${WORKBENCH_UPLOAD_PROMPT:-我刚上传了新的证据，请自动重算并刷新案由、法条、类案。}"
WS_PROBE_PROMPT="${WORKBENCH_WS_PROBE_PROMPT:-请回复“WS稳定性检查OK”。}"
EVIDENCE_FILE="${WORKBENCH_EVIDENCE_FILE:-$E2E_ROOT/tests/lawyer_workbench/civil_prosecution/evidence/bus_ticket.txt}"
PW_HEADED="${PW_HEADED:-0}"

mkdir -p "$OUT_ROOT"

python3 - "$OUT_ROOT" "$WORKBENCH_URL" "$PWCLI" "$LAWYER_USER" "$LAWYER_PASS" "$FIRST_PROMPT" "$UPLOAD_PROMPT" "$WS_PROBE_PROMPT" "$EVIDENCE_FILE" "$PW_HEADED" <<'PY'
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

out_root = Path(sys.argv[1]).resolve()
workbench_url = sys.argv[2]
pwcli = sys.argv[3]
lawyer_user = sys.argv[4]
lawyer_pass = sys.argv[5]
first_prompt = sys.argv[6]
upload_prompt = sys.argv[7]
ws_probe_prompt = sys.argv[8]
evidence_file = Path(sys.argv[9]).resolve()
pw_headed = sys.argv[10] == "1"

logs_dir = out_root / "logs"
json_dir = out_root / "json"
shots_dir = out_root / "screenshots"
for d in (logs_dir, json_dir, shots_dir):
    d.mkdir(parents=True, exist_ok=True)

session_name = f"hc{int(time.time()) % 100000}"


def run_pw(*args: str) -> str:
    cmd = [pwcli, "--session", session_name, *args]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"Playwright CLI failed: {' '.join(cmd)}\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}")
    return cp.stdout


def parse_run_code_result(text: str) -> dict:
    m = re.search(r"### Result\n(.*?)\n### Ran Playwright code", text, flags=re.S)
    if not m:
        return {"_parse_error": "result_not_found", "_raw": text[:4000]}
    body = m.group(1).strip()
    if not body:
        return {}
    try:
        return json.loads(body)
    except Exception:
        return {"_raw_result": body}


def run_code(label: str, js: str) -> dict:
    wrapped = f"async (page) => {{ {js} }}"
    out = run_pw("run-code", wrapped)
    (logs_dir / f"{label}.run-code.txt").write_text(out, encoding="utf-8")
    payload = parse_run_code_result(out)
    (json_dir / f"{label}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def snapshot(label: str) -> None:
    snap = run_pw("snapshot")
    (logs_dir / f"{label}.snapshot.txt").write_text(snap, encoding="utf-8")
    shot_path = shots_dir / f"{label}.png"
    run_code(
        f"{label}_screenshot",
        f"await page.screenshot({{ path: {json.dumps(str(shot_path))}, fullPage: true }}); return {{ url: page.url(), title: await page.title() }};",
    )


def switch_tab(tab_label: str) -> None:
    run_code(
        f"switch_tab_{tab_label}",
        f"""
let clicked = false;
const candidates = page.locator('button', {{ hasText: {json.dumps(tab_label)} }});
if ((await candidates.count()) > 0) {{
  await candidates.first().click();
  clicked = true;
  await page.waitForTimeout(700);
}}
return {{ tab: {json.dumps(tab_label)}, clicked }};
""",
    )


def send_message(label: str, prompt: str, wait_ms: int = 9000) -> None:
    run_code(
        label,
        f"""
let sent = false;
const dialog = page.getByRole('dialog', {{ name: /法学助手/ }});
if ((await dialog.count()) === 0) {{
  const candidates = [
    page.getByTestId('assistant-floating-button'),
    page.getByRole('button', {{ name: /法学助手|智能助手|assistant/i }}),
    page.locator('[aria-label*=助手]'),
  ];
  for (const btn of candidates) {{
    if ((await btn.count()) > 0) {{
      await btn.first().click();
      await page.waitForTimeout(700);
      break;
    }}
  }}
}}
if ((await dialog.count()) > 0) {{
  const input = page.getByTestId('workbench-chat-input');
  if ((await input.count()) > 0) {{
    await input.first().fill({json.dumps(prompt)});
    const send = dialog.getByRole('button', {{ name: /发送/ }});
    if ((await send.count()) > 0) {{
      await send.first().click();
      sent = true;
      await page.waitForTimeout({wait_ms});
    }}
  }}
}}
return {{ sent }};
""",
    )


COLLECT_STATE_JS = """
return await page.evaluate(() => {
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const root = document;
  const bodyText = normalize(root.body ? root.body.textContent || '' : '');

  const basics = root.querySelector('[data-testid="workbench-case-basics-card"]');
  const readRow = (label) => {
    if (!basics) return '';
    const rows = Array.from(basics.querySelectorAll('div'));
    for (const row of rows) {
      const text = normalize(row.textContent || '');
      if (!text.includes(label)) continue;
      const spans = row.querySelectorAll('span');
      if (spans.length >= 2) return normalize(spans[1].textContent || '');
    }
    return '';
  };

  const causeCards = Array.from(root.querySelectorAll('[data-testid="workbench-cause-card"]'));
  const confirmedCards = causeCards.filter((card) => normalize(card.textContent || '').includes('当前生效案由'));
  const causeCountFromText = (() => {
    const match = bodyText.match(/候选案由\s*(\d+)\s*条/);
    if (!match) return 0;
    const n = Number(match[1]);
    return Number.isFinite(n) ? n : 0;
  })();

  const lawSection = root.querySelector('[data-testid="workbench-section-laws"]');
  const lawCards = Array.from(root.querySelectorAll('[data-testid="workbench-law-card"]'));
  const firstLawText = lawCards.length ? normalize(lawCards[0].textContent || '') : '';
  let legalEmptyReason = '';
  if (lawSection && lawCards.length === 0) {
    const cands = Array.from(lawSection.querySelectorAll('div,span,p')).map((el) => normalize(el.textContent || '')).filter(Boolean);
    legalEmptyReason = cands.find((txt) => txt.includes('案由未确认') || txt.includes('证据不足') || txt.includes('检索无命中') || txt.includes('暂无')) || '';
  }

  const caseSection = root.querySelector('[data-testid="workbench-section-cases"]');
  const caseCards = Array.from(root.querySelectorAll('[data-testid="workbench-case-card"]'));
  const firstCaseText = caseCards.length ? normalize(caseCards[0].textContent || '') : '';
  let caseEmptyReason = '';
  if (caseSection && caseCards.length === 0) {
    const cands = Array.from(caseSection.querySelectorAll('div,span,p')).map((el) => normalize(el.textContent || '')).filter(Boolean);
    caseEmptyReason = cands.find((txt) => txt.includes('案由未确认') || txt.includes('证据不足') || txt.includes('检索无命中') || txt.includes('暂无')) || '';
  }

  const aside = root.querySelector('[data-testid="workbench-profile-aside"]');
  const asideText = normalize(aside ? aside.textContent || '' : '');
  const statusBadge = basics ? normalize((basics.querySelector('span.px-2') || basics.querySelector('span'))?.textContent || '') : '';

  const legacyContractDetected = bodyText.includes('推荐度') || bodyText.includes('证据支撑度') || bodyText.includes('待确认卡片');

  return {
    url: location.href,
    ts: new Date().toISOString(),
    legacy_contract_detected: legacyContractDetected,
    body_preview: bodyText.slice(0, 800),
    basics: {
      visible: Boolean(basics),
      status: statusBadge,
      service_type: readRow('服务类型'),
      procedure_stage: readRow('程序阶段'),
      my_role: readRow('我方角色'),
      plaintiff: readRow('原告'),
      defendant: readRow('被告'),
      other_parties: readRow('其他当事人'),
      recomputed_at: readRow('最近重算'),
    },
    cause: {
      count: causeCards.length > 0 ? causeCards.length : causeCountFromText,
      confirmed_count: confirmedCards.length,
      first_is_confirmed: causeCards.length > 0 ? normalize(causeCards[0].textContent || '').includes('当前生效案由') : bodyText.includes('当前生效案由'),
      first_card_text: causeCards.length > 0 ? normalize(causeCards[0].textContent || '') : '',
      has_detail_button: causeCards.some((card) => Array.from(card.querySelectorAll('button')).some((btn) => normalize(btn.textContent || '').includes('判定详情'))) || bodyText.includes('判定详情'),
    },
    references: {
      legal_count: lawCards.length,
      legal_first_has_article: firstLawText.includes('条') || firstLawText.includes('款'),
      legal_first_has_reason: firstLawText.includes('适用说明'),
      legal_empty_reason: legalEmptyReason,
      case_count: caseCards.length,
      case_first_has_case_no: firstCaseText.includes('案号') || /\d{4}/.test(firstCaseText),
      case_first_has_similarity: firstCaseText.includes('相似点'),
      case_empty_reason: caseEmptyReason,
    },
    strategy: {
      locked: asideText.includes('待案由确认'),
      prediction_locked: asideText.includes('待案由确认'),
      has_strategy_module: asideText.includes('诉讼策略') || bodyText.includes('诉讼策略'),
    },
  };
});
"""

CAUSE_DETAIL_JS = """
let clicked = false;
const cards = page.locator('[data-testid="workbench-cause-card"]');
if ((await cards.count()) > 0) {
  const detailBtn = cards.nth(0).getByRole('button', { name: /判定详情/ });
  if ((await detailBtn.count()) > 0) {
    await detailBtn.first().click();
    clicked = true;
    await page.waitForTimeout(450);
  }
}
const out = await page.evaluate(() => {
  const modal = document.querySelector('[data-testid="workbench-cause-detail-modal"]');
  const text = String(modal?.textContent || '').replace(/\s+/g, ' ').trim();
  return {
    visible: Boolean(modal),
    has_reason: text.includes('推荐理由'),
    has_supporting_facts: text.includes('支持事实'),
    has_supporting_evidence: text.includes('支持证据'),
    has_missing_materials: text.includes('缺失材料'),
    has_risk_flags: text.includes('风险提示'),
    has_counter_reasons: text.includes('冲突/反向理由') || text.includes('反向理由'),
  };
});
out.clicked = clicked;
if (out.visible) {
  const closeBtn = page.locator('[data-testid="workbench-cause-detail-modal"] button');
  if ((await closeBtn.count()) > 0) {
    await closeBtn.first().click();
    await page.waitForTimeout(250);
  }
}
return out;
"""

TRACE_MODAL_JS = """
let clicked = false;
const btn = page.locator('button', { hasText: '查看分析过程' });
if ((await btn.count()) > 0) {
  await btn.first().click();
  clicked = true;
  await page.waitForTimeout(450);
}
const out = await page.evaluate(() => {
  const modal = document.querySelector('[data-testid="workbench-analysis-trace-modal"]');
  const text = String(modal?.textContent || '').replace(/\s+/g, ' ').trim();
  return {
    visible: Boolean(modal),
    has_selected_cause: text.includes('当前选定案由'),
    has_risk_source: text.includes('诉讼风险来源'),
    has_win_rate_source: text.includes('胜率来源'),
    has_skills_timeline: text.includes('技能执行时间线'),
  };
});
out.clicked = clicked;
if (out.visible) {
  const closeBtn = page.locator('[data-testid="workbench-analysis-trace-modal"] button');
  if ((await closeBtn.count()) > 0) {
    await closeBtn.first().click();
    await page.waitForTimeout(250);
  }
}
return out;
"""

CITATION_TRACE_JS = """
let clicked = false;
const btn = page.locator('[data-testid="workbench-section-references"] button', { hasText: '查看引用' });
if ((await btn.count()) > 0) {
  await btn.first().click();
  clicked = true;
  await page.waitForTimeout(500);
}
const out = await page.evaluate(() => {
  const citation = document.querySelector('[data-testid="workbench-section-citations"]');
  const cards = citation ? Array.from(citation.querySelectorAll('div.rounded-lg.border.border-slate-200.bg-white')) : [];
  return {
    citation_cards: cards.length,
    citation_section_text: String(citation?.textContent || '').replace(/\s+/g, ' ').trim(),
  };
});
out.clicked = clicked;
return out;
"""

WS_STABILITY_JS = """
let toggled = false;
let toggle_error = '';
try {
  await page.context().setOffline(true);
  await page.waitForTimeout(1200);
  await page.context().setOffline(false);
  await page.waitForTimeout(1200);
  toggled = true;
} catch (err) {
  toggle_error = String(err && err.message ? err.message : err || '');
}
const dialog = page.getByRole('dialog', { name: /法学助手/ });
let assistant = [];
let blank = 0;
if ((await dialog.count()) > 0) {
  const bubbles = dialog.locator('div.bg-white.border.border-slate-200.px-4.py-3.rounded-2xl.rounded-tl-none');
  const count = await bubbles.count();
  for (let i = 0; i < count; i += 1) {
    const text = (await bubbles.nth(i).innerText()).trim();
    if (!text) {
      blank += 1;
    } else {
      assistant.push(text);
    }
  }
}
let dup = 0;
for (let i = 1; i < assistant.length; i += 1) {
  if (assistant[i] === assistant[i - 1]) dup += 1;
}
return {
  toggled,
  toggle_error: toggle_error || null,
  assistant_message_count: assistant.length,
  blank_message_count: blank,
  consecutive_duplicates: dup,
};
"""

ISOLATION_JS = """
const currentHref = page.url();
const parseParams = (href) => {
  const q = href.includes('?') ? href.split('?')[1] : '';
  const map = new Map();
  for (const pair of q.split('&')) {
    if (!pair) continue;
    const [k, v] = pair.split('=');
    const key = decodeURIComponent(String(k || '').trim());
    if (!key) continue;
    map.set(key, decodeURIComponent(String(v || '').trim()));
  }
  return map;
};
const params = parseParams(currentHref);
const matterId = params.get('matter_id') || '';
const sessionId = params.get('session_id') || '';
if (!matterId || !sessionId) {
  return { skipped: true, reason: 'missing_matter_or_session' };
}
let basicsText = '';
const basicsCard = page.locator('[data-testid="workbench-case-basics-card"]');
if ((await basicsCard.count()) > 0) {
  basicsText = (await basicsCard.first().innerText()).replace(/\s+/g, ' ').trim();
}
const tokens = basicsText
  .split(/\s+/)
  .map((t) => t.trim())
  .filter((t) => t.length >= 2 && !['服务类型', '程序阶段', '我方角色', '原告', '被告', '其他当事人', '最近重算'].includes(t))
  .slice(0, 6);

const base = currentHref.split('?')[0] || currentHref;
params.set('matter_id', String(Number(matterId) + 999999));
params.set('session_id', String(Number(sessionId) + 999999));
const query = Array.from(params.entries())
  .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
  .join('&');
const wrongUrl = query ? `${base}?${query}` : base;

const p = await page.context().newPage();
await p.goto(wrongUrl, { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(2800);
const hasBasics = (await p.locator('[data-testid="workbench-case-basics-card"]').count()) > 0;
const body = (await p.locator('body').innerText()).replace(/\s+/g, ' ').trim();
await p.close();
const leaked = tokens.some((token) => token && body.includes(token));
return {
  skipped: false,
  wrong_url: wrongUrl,
  has_basics: hasBasics,
  leaked,
  sensitive_tokens: tokens,
};
"""


def load_json(name: str) -> dict:
    p = json_dir / f"{name}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


print(f"[INFO] Playwright session: {session_name}")
print(f"[INFO] URL: {workbench_url}")
print(f"[INFO] Artifacts: {out_root}")

open_args = ["open", workbench_url]
if pw_headed:
    open_args.append("--headed")
open_out = run_pw(*open_args)
(logs_dir / "open.txt").write_text(open_out, encoding="utf-8")

snapshot("step-01-open")

run_code(
    "step-02-login",
    f"""
let attempted = false;
let logged_in = false;
const trigger = page.getByRole('button', {{ name: /登录\\s*\\/\\s*注册|登录|注册/ }});
if ((await trigger.count()) > 0 && await trigger.first().isVisible()) {{
  attempted = true;
  await trigger.first().click();
  await page.waitForTimeout(900);
}}
const user = page.getByLabel('用户名');
const phone = page.getByLabel('手机号');
const pass = page.getByLabel('密码');
if ((await user.count()) > 0) {{
  attempted = true;
  await user.first().fill({json.dumps(lawyer_user)});
  if ((await pass.count()) > 0) {{
    await pass.first().fill({json.dumps(lawyer_pass)});
  }}
  const submit = page.getByRole('button', {{ name: /登录/ }});
  if ((await submit.count()) > 0) {{
    await submit.first().click();
    await page.waitForTimeout(4500);
    logged_in = true;
  }}
}} else if ((await phone.count()) > 0) {{
  attempted = true;
  await phone.first().fill({json.dumps(lawyer_user)});
}}
await page.goto({json.dumps(workbench_url)}, {{ waitUntil: 'domcontentloaded' }});
await page.waitForTimeout(3500);
return {{ attempted, logged_in, url: page.url() }};
""",
)
snapshot("step-02-login")

run_code(
    "step-03-entry",
    f"""
let hasBasics = (await page.locator('[data-testid="workbench-case-basics-card"]').count()) > 0;
if (!hasBasics) {{
  const input = page.getByPlaceholder('请输入案件描述或上传文件进行分析...');
  if ((await input.count()) > 0) {{
    await input.first().fill({json.dumps(first_prompt)});
  }}
  const start = page.getByRole('button', {{ name: /开始分析/ }});
  if ((await start.count()) > 0) {{
    await start.first().click();
    await page.waitForTimeout(9000);
  }}
  hasBasics = (await page.locator('[data-testid="workbench-case-basics-card"]').count()) > 0;
}}
return {{ has_basics_after: hasBasics }};
""",
)
snapshot("step-03-entry")

run_code("state_overview", COLLECT_STATE_JS)
switch_tab("分析")
snapshot("step-04-analysis")
run_code("state_analysis", COLLECT_STATE_JS)

switch_tab("总览")
run_code("state_cause_detail", CAUSE_DETAIL_JS)
run_code("state_trace_modal", TRACE_MODAL_JS)

switch_tab("分析")
run_code("state_citation_trace", CITATION_TRACE_JS)
run_code("state_pre_upload", COLLECT_STATE_JS)

send_message("step-05-first-message", first_prompt)

if evidence_file.exists():
    run_code(
        "step-06-upload-evidence",
        f"""
let uploaded = false;
let upload_error = null;
try {{
  const inputs = page.locator('input[type=file]');
  const count = await inputs.count();
  if (count > 0) {{
    await inputs.nth(count - 1).setInputFiles({json.dumps(str(evidence_file))});
    uploaded = true;
    await page.waitForTimeout(1400);
  }}
}} catch (err) {{
  upload_error = String(err && err.message ? err.message : err || '');
}}
return {{ uploaded, upload_error }};
""",
    )
else:
    (json_dir / "step-06-upload-evidence.json").write_text(
        json.dumps({"uploaded": False, "upload_error": "evidence_file_not_found"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

send_message("step-07-upload-prompt", upload_prompt)
snapshot("step-07-after-upload")
run_code("state_post_upload", COLLECT_STATE_JS)

send_message("step-08-ws-probe", ws_probe_prompt, wait_ms=7000)
run_code("state_ws_stability", WS_STABILITY_JS)
run_code("state_isolation", ISOLATION_JS)

pre = load_json("state_pre_upload")
post = load_json("state_post_upload")
changed = {
    "recomputed_at_changed": ((pre.get("basics") or {}).get("recomputed_at") or "") != ((post.get("basics") or {}).get("recomputed_at") or ""),
    "cause_first_changed": ((pre.get("cause") or {}).get("first_card_text") or "") != ((post.get("cause") or {}).get("first_card_text") or ""),
    "legal_count_changed": ((pre.get("references") or {}).get("legal_count") or 0) != ((post.get("references") or {}).get("legal_count") or 0),
    "case_count_changed": ((pre.get("references") or {}).get("case_count") or 0) != ((post.get("references") or {}).get("case_count") or 0),
}
changed["any_changed"] = any(changed.values())
compare = {"pre": pre, "post": post, "changed": changed}
(json_dir / "recompute_compare.json").write_text(json.dumps(compare, ensure_ascii=False, indent=2), encoding="utf-8")

summary = {
    "workbench_url": workbench_url,
    "playwright_session": session_name,
    "artifact_root": str(out_root),
    "json_files": sorted([p.name for p in json_dir.glob("*.json")]),
    "screenshots": sorted([p.name for p in shots_dir.glob("*.png")]),
    "logs": sorted([p.name for p in logs_dir.glob("*")]),
}
(out_root / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"[OK] Hardcut Playwright artifacts: {out_root}")
print(f"[NEXT] python3 scripts/assert_workbench_hardcut_results.py --artifacts {out_root}")
PY

echo "[DONE] run_workbench_hardcut_playwright_cli.sh"
