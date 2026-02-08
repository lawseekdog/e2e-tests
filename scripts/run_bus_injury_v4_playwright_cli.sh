#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$E2E_ROOT/.." && pwd)"

if ! command -v npx >/dev/null 2>&1; then
  echo "[ERROR] npx is required. Install Node.js/npm first." >&2
  exit 1
fi

export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export PWCLI="${PWCLI:-$CODEX_HOME/skills/playwright/scripts/playwright_cli.sh}"
if [[ ! -x "$PWCLI" ]]; then
  echo "[ERROR] Playwright CLI wrapper not found: $PWCLI" >&2
  exit 1
fi

OUT_BASE="${1:-$REPO_ROOT/output/e2e/bus-injury-v4/$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="$OUT_BASE/ui"
mkdir -p "$OUT_DIR"

if [[ -f "$E2E_ROOT/.env" ]]; then
  # shellcheck disable=SC1090
  source "$E2E_ROOT/.env"
fi

WORKBENCH_URL="${WORKBENCH_UI_URL:-${PW_WORKBENCH_URL:-http://localhost/workbench}}"
LAWYER_USER="${LAWYER_USERNAME:-lawyer1}"
LAWYER_PASS="${LAWYER_PASSWORD:-lawyer123456}"
FIRST_INPUT="${BUS_INJURY_FIRST_INPUT:-张三坐公交车受伤了}"
REFS_PROMPT="${BUS_INJURY_REFS_PROMPT:-请先给候选法条和类案，标注待证据校验}"
EVIDENCE_PROMPT="${BUS_INJURY_EVIDENCE_PROMPT:-补充证据：急刹+受伤经过。}"
EVIDENCE_FILE="${BUS_INJURY_EVIDENCE_FILE:-$E2E_ROOT/tests/lawyer_workbench/civil_prosecution/evidence/bus_ticket.txt}"

SESSION_NAME="b$(date +%s)"
PW=("$PWCLI" --session "$SESSION_NAME")

run_code() {
  local code="$1"
  local wrapped="async (page) => { ${code} }"
  "${PW[@]}" run-code "$wrapped"
}

snapshot_to() {
  local file="$1"
  "${PW[@]}" snapshot > "$file"
}

echo "[INFO] output: $OUT_BASE"
echo "[INFO] opening: $WORKBENCH_URL"
"${PW[@]}" open "$WORKBENCH_URL" --headed
snapshot_to "$OUT_DIR/step0-open.snapshot.txt"

run_code "
const loginBtn = page.getByRole('button', { name: /登录\s*\/\s*注册|登录|注册/ });
if (await loginBtn.count() > 0 && await loginBtn.first().isVisible()) {
  await loginBtn.first().click();
  await page.waitForTimeout(1200);
}
const userByName = page.getByLabel('用户名');
const userByPhone = page.getByLabel('手机号');
if (await userByName.count() > 0) {
  await userByName.first().fill('$LAWYER_USER');
  const pass = page.getByLabel('密码');
  if (await pass.count() > 0) {
    await pass.first().fill('$LAWYER_PASS');
  }
  const submit = page.getByRole('button', { name: /登录/ });
  if (await submit.count() > 0) {
    await submit.first().click();
    await page.waitForTimeout(5000);
  }
} else if (await userByPhone.count() > 0) {
  await userByPhone.first().fill('$LAWYER_USER');
}
await page.screenshot({ path: '$OUT_DIR/ui-step-1-login.png', fullPage: true });
"

run_code "
const caseTab = page.getByRole('button', { name: /案件分析/ });
if (await caseTab.count() > 0 && await caseTab.first().isVisible()) {
  await caseTab.first().click();
}
const input = page.getByPlaceholder('请输入案件描述或上传文件进行分析...');
if (await input.count() > 0) {
  await input.first().fill('$FIRST_INPUT');
}
const startBtn = page.getByRole('button', { name: /开始分析/ });
if (await startBtn.count() > 0 && await startBtn.first().isVisible()) {
  await startBtn.first().click();
}
await page.waitForTimeout(5000);
await page.screenshot({ path: '$OUT_DIR/ui-step-2-entry.png', fullPage: true });
"

snapshot_to "$OUT_DIR/step1-after-entry.snapshot.txt"

run_code "
const assistantBtn = page.getByTestId('assistant-floating-button');
if (await assistantBtn.count() > 0) {
  await assistantBtn.first().click();
  await page.waitForTimeout(800);
}
const chatInput = page.getByTestId('workbench-chat-input');
if (await chatInput.count() > 0) {
  await chatInput.first().fill('$FIRST_INPUT');
  const sendBtn = page.getByRole('button', { name: /发送/ });
  if (await sendBtn.count() > 0) {
    await sendBtn.first().click();
    await page.waitForTimeout(12000);
  }
}
await page.screenshot({ path: '$OUT_DIR/ui-step-3-first-response.png', fullPage: true });
"

snapshot_to "$OUT_DIR/step2-after-first-chat.snapshot.txt"

run_code "
const chatInput = page.getByTestId('workbench-chat-input');
if (await chatInput.count() > 0) {
  await chatInput.first().fill('$REFS_PROMPT');
  const sendBtn = page.getByRole('button', { name: /发送/ });
  if (await sendBtn.count() > 0) {
    await sendBtn.first().click();
    await page.waitForTimeout(10000);
  }
}
await page.screenshot({ path: '$OUT_DIR/ui-step-4-cause-refs.png', fullPage: true });
"

if [[ -f "$EVIDENCE_FILE" ]]; then
  run_code "
  const evidenceInput = page.locator('input[type=file]').first();
  if (await evidenceInput.count() > 0) {
    await evidenceInput.setInputFiles('$EVIDENCE_FILE');
    await page.waitForTimeout(3000);
  }
  await page.screenshot({ path: '$OUT_DIR/ui-step-5-after-upload.png', fullPage: true });
  "

  run_code "
  const chatInput = page.getByTestId('workbench-chat-input');
  if (await chatInput.count() > 0) {
    await chatInput.first().fill('$EVIDENCE_PROMPT');
    const sendBtn = page.getByRole('button', { name: /发送/ });
    if (await sendBtn.count() > 0) {
      await sendBtn.first().click();
      await page.waitForTimeout(10000);
    }
  }
  await page.screenshot({ path: '$OUT_DIR/ui-step-6-final.png', fullPage: true });
  "
else
  echo "[WARN] evidence file not found: $EVIDENCE_FILE"
  run_code "await page.screenshot({ path: '$OUT_DIR/ui-step-5-after-upload.png', fullPage: true });"
  run_code "await page.screenshot({ path: '$OUT_DIR/ui-step-6-final.png', fullPage: true });"
fi

snapshot_to "$OUT_DIR/step3-final.snapshot.txt"

cat > "$OUT_BASE/layer-b-summary.md" <<EOF
# Layer B Playwright CLI Summary

- workbench_url: $WORKBENCH_URL
- session: $SESSION_NAME
- first_input: $FIRST_INPUT
- refs_prompt: $REFS_PROMPT
- evidence_file: $EVIDENCE_FILE

## Artifacts

- $OUT_DIR/ui-step-1-login.png
- $OUT_DIR/ui-step-2-entry.png
- $OUT_DIR/ui-step-3-first-response.png
- $OUT_DIR/ui-step-4-cause-refs.png
- $OUT_DIR/ui-step-5-after-upload.png
- $OUT_DIR/ui-step-6-final.png
- $OUT_DIR/step0-open.snapshot.txt
- $OUT_DIR/step1-after-entry.snapshot.txt
- $OUT_DIR/step2-after-first-chat.snapshot.txt
- $OUT_DIR/step3-final.snapshot.txt
EOF

echo "[OK] Layer B artifacts written to $OUT_BASE"
