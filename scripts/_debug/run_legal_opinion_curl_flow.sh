#!/usr/bin/env bash
set -euo pipefail

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$E2E_ROOT/.." && pwd)"

BASE_URL="${BASE_URL:-http://127.0.0.1:18080/api/v1}"
CONSULTATIONS_INTERNAL_BASE_URL="${CONSULTATIONS_INTERNAL_BASE_URL:-http://127.0.0.1:18103/api/v1}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/infra-live/.local/env.local}"
USERNAME="${LAWYER_USERNAME:-admin}"
PASSWORD="${LAWYER_PASSWORD:-admin123456}"
SERVICE_TYPE_ID="${SERVICE_TYPE_ID:-legal_opinion}"
CLIENT_ROLE="${CLIENT_ROLE:-applicant}"
KICKOFF_TEXT="${KICKOFF_TEXT:-请基于已上传材料形成一份结构化法律意见分析，输出结论、风险与行动建议。}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/output/legal-opinion-curl/$(date '+%Y%m%d-%H%M%S')}"

FIXTURES=(
  "$E2E_ROOT/scripts/_support/fixtures/legal_opinion_supply_contract.txt"
  "$E2E_ROOT/scripts/_support/fixtures/legal_opinion_performance_timeline.txt"
  "$E2E_ROOT/scripts/_support/fixtures/legal_opinion_demand_reply.txt"
)

command -v curl >/dev/null 2>&1 || die "缺少命令：curl"
command -v jq >/dev/null 2>&1 || die "缺少命令：jq"
[[ -f "$ENV_FILE" ]] || die "缺少 env 文件：$ENV_FILE"

mkdir -p "$OUTPUT_DIR"

write_json() {
  local path="$1"
  local payload="$2"
  printf '%s' "$payload" >"$path"
}

json_get() {
  local expr="$1"
  local path="$2"
  jq -r "$expr // empty" "$path"
}

request() {
  curl -sS "$@"
}

INTERNAL_API_KEY="$(grep '^INTERNAL_API_KEY=' "$ENV_FILE" | head -n1 | cut -d= -f2- || true)"
[[ -n "$INTERNAL_API_KEY" ]] || die "INTERNAL_API_KEY 为空"

log "登录用户 $USERNAME"
LOGIN_JSON="$(
  request -X POST "$BASE_URL/auth-service/auth/login" \
    -H 'Content-Type: application/json' \
    --data "$(jq -nc --arg username "$USERNAME" --arg password "$PASSWORD" '{username:$username,password:$password}')"
)"
write_json "$OUTPUT_DIR/login.json" "$LOGIN_JSON"

TOKEN="$(json_get '.data.access_token' "$OUTPUT_DIR/login.json")"
[[ -n "$TOKEN" ]] || die "登录失败，未获取到 access_token"

ME_JSON="$(request "$BASE_URL/auth-service/auth/me" -H "Authorization: Bearer $TOKEN")"
write_json "$OUTPUT_DIR/me.json" "$ME_JSON"
USER_ID="$(json_get '.data.user_id' "$OUTPUT_DIR/me.json")"
ORGANIZATION_ID="$(json_get '.data.organization_id' "$OUTPUT_DIR/me.json")"
IS_SUPERUSER="$(json_get '.data.is_superuser' "$OUTPUT_DIR/me.json")"
[[ -n "$USER_ID" && -n "$ORGANIZATION_ID" ]] || die "auth/me 未返回 user_id 或 organization_id"

AUTH_HEADER=(
  -H "Authorization: Bearer $TOKEN"
  -H "X-User-Id: $USER_ID"
  -H "X-Organization-Id: $ORGANIZATION_ID"
)
if [[ "$(printf '%s' "$IS_SUPERUSER" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
  AUTH_HEADER+=( -H "X-Is-Superuser: true" )
fi

FILE_IDS=()
index=0
for fixture in "${FIXTURES[@]}"; do
  [[ -f "$fixture" ]] || die "缺少 fixture：$fixture"
  index=$((index + 1))
  log "上传材料 $(basename "$fixture")"
  upload_json="$(
    request -X POST "$BASE_URL/files-service/files/upload?purpose=consultation&user_id=$USER_ID" \
      "${AUTH_HEADER[@]}" \
      -F "file=@${fixture}"
  )"
  write_json "$OUTPUT_DIR/upload-${index}.json" "$upload_json"
  file_id="$(json_get '.data.id' "$OUTPUT_DIR/upload-${index}.json")"
  [[ -n "$file_id" ]] || die "上传失败：$fixture"
  FILE_IDS+=("$file_id")
done

FILE_IDS_JSON="$(printf '%s\n' "${FILE_IDS[@]}" | jq -R . | jq -s .)"

log "创建事项 service_type_id=$SERVICE_TYPE_ID"
MATTER_JSON="$(
  request -X POST "$BASE_URL/matter-service/lawyer/matters" \
    "${AUTH_HEADER[@]}" \
    -H 'Content-Type: application/json' \
    --data "$(
      jq -nc \
        --arg title "curl legal opinion debug" \
        --arg service_type_id "$SERVICE_TYPE_ID" \
        --arg client_role "$CLIENT_ROLE" \
        --argjson file_ids "$FILE_IDS_JSON" \
        '{title:$title,service_type_id:$service_type_id,client_role:$client_role,file_ids:$file_ids}'
    )"
)"
write_json "$OUTPUT_DIR/matter-create.json" "$MATTER_JSON"
MATTER_ID="$(json_get '.data.id' "$OUTPUT_DIR/matter-create.json")"
[[ -n "$MATTER_ID" ]] || die "创建事项失败"

log "创建会话 matter_id=$MATTER_ID"
SESSION_JSON="$(
  request -X POST "$BASE_URL/consultations-service/consultations/sessions" \
    "${AUTH_HEADER[@]}" \
    -H 'Content-Type: application/json' \
    --data "$(
      jq -nc \
        --arg title "curl legal opinion session" \
        --argjson matter_id "$MATTER_ID" \
        '{title:$title,matter_id:$matter_id}'
    )"
)"
write_json "$OUTPUT_DIR/session-create.json" "$SESSION_JSON"
SESSION_ID="$(json_get '.data.id' "$OUTPUT_DIR/session-create.json")"
[[ -n "$SESSION_ID" ]] || die "创建会话失败"

log "触发内部 chat session_id=$SESSION_ID"
CHAT_JSON="$(
  request -X POST "$CONSULTATIONS_INTERNAL_BASE_URL/internal/sessions/${SESSION_ID}/messages/user-and-chat" \
    -H 'Content-Type: application/json' \
    -H "X-Internal-Api-Key: $INTERNAL_API_KEY" \
    -H "X-Organization-Id: $ORGANIZATION_ID" \
    --data "$(
      jq -nc \
        --arg content "$KICKOFF_TEXT" \
        --argjson user_id "$USER_ID" \
        --arg matter_id "$MATTER_ID" \
        --argjson attachments "$FILE_IDS_JSON" \
        '{content:$content,user_id:$user_id,matter_id:$matter_id,attachments:$attachments}'
    )"
)"
write_json "$OUTPUT_DIR/chat-1.json" "$CHAT_JSON"

PENDING_JSON="$(request "$BASE_URL/consultations-service/consultations/sessions/${SESSION_ID}/pending_card" "${AUTH_HEADER[@]}")"
SNAPSHOT_JSON="$(request "$BASE_URL/matter-service/lawyer/matters/${MATTER_ID}/workbench/snapshot" "${AUTH_HEADER[@]}")"
DELIVERABLES_JSON="$(request "$BASE_URL/matter-service/lawyer/matters/${MATTER_ID}/deliverables" "${AUTH_HEADER[@]}")"
MESSAGES_JSON="$(request "$BASE_URL/consultations-service/consultations/sessions/${SESSION_ID}/messages?page=1&size=100" "${AUTH_HEADER[@]}")"

write_json "$OUTPUT_DIR/pending-1.json" "$PENDING_JSON"
write_json "$OUTPUT_DIR/snapshot-1.json" "$SNAPSHOT_JSON"
write_json "$OUTPUT_DIR/deliverables-1.json" "$DELIVERABLES_JSON"
write_json "$OUTPUT_DIR/messages-1.json" "$MESSAGES_JSON"

jq -nc \
  --arg output_dir "$OUTPUT_DIR" \
  --arg matter_id "$MATTER_ID" \
  --arg session_id "$SESSION_ID" \
  --argjson file_ids "$FILE_IDS_JSON" \
  --arg chat_success "$(json_get '.data.success' "$OUTPUT_DIR/chat-1.json")" \
  --arg chat_error "$(json_get '.data.error' "$OUTPUT_DIR/chat-1.json")" \
  --arg output_preview "$(json_get '.data.output' "$OUTPUT_DIR/chat-1.json" | cut -c 1-200)" \
  --arg card_id "$(json_get '.data.card.id' "$OUTPUT_DIR/chat-1.json")" \
  --arg card_skill_id "$(json_get '.data.card.skill_id' "$OUTPUT_DIR/chat-1.json")" \
  --arg pending_card_id "$(json_get '.data.id' "$OUTPUT_DIR/pending-1.json")" \
  --arg pending_skill_id "$(json_get '.data.skill_id' "$OUTPUT_DIR/pending-1.json")" \
  --arg pending_type "$(json_get '.data.type' "$OUTPUT_DIR/pending-1.json")" \
  --arg deliverable_count "$(jq -r '(.data.deliverables // []) | length' "$OUTPUT_DIR/deliverables-1.json")" \
  --arg message_count "$(jq -r '(.data.data // []) | length' "$OUTPUT_DIR/messages-1.json")" \
  '{
    output_dir: $output_dir,
    matter_id: $matter_id,
    session_id: $session_id,
    file_ids: $file_ids,
    chat_success: $chat_success,
    chat_error: $chat_error,
    output_preview: $output_preview,
    card_id: $card_id,
    card_skill_id: $card_skill_id,
    pending_card_id: $pending_card_id,
    pending_skill_id: $pending_skill_id,
    pending_type: $pending_type,
    deliverable_count: $deliverable_count,
    message_count: $message_count
  }' | tee "$OUTPUT_DIR/summary.json"
