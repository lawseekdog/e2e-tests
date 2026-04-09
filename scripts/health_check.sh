#!/bin/bash
# 健康检查脚本

set -e

BASE_URL="${BASE_URL:-http://localhost:18001/api/v1}"
INTERNAL_API_KEY="${INTERNAL_API_KEY:-change-me-in-production-32chars}"
REMOTE_STACK_HOST="${LAWSEEKDOG_REMOTE_STACK_HOST:-${REMOTE_STACK_HOST:-8.148.207.157}}"
E2E_USE_GATEWAY="${E2E_USE_GATEWAY:-0}"

BASE_URL="${BASE_URL%/}"
# APISIX exposes a plain health endpoint at the gateway root.
GATEWAY_ROOT="${BASE_URL%/api/v1}"
if [ "$GATEWAY_ROOT" == "$BASE_URL" ]; then
  GATEWAY_ROOT="$BASE_URL"
fi

SERVICES=(
  "auth-service"
  "user-service"
  "organization-service"
  "billing-service"
  "consultations-service"
  "matter-service"
  "knowledge-service"
  "files-service"
  "platform-service"
  "templates-service"
  "collector-service"
  "notification-service"
  "memory-service"
)

service_base_url() {
  local service="$1"
  case "$service" in
    auth-service) printf '%s' "${E2E_AUTH_BASE_URL:-http://${REMOTE_STACK_HOST}:18101/api/v1}" ;;
    user-service) printf '%s' "${E2E_USER_BASE_URL:-http://${REMOTE_STACK_HOST}:18113/api/v1}" ;;
    organization-service) printf '%s' "${E2E_ORG_BASE_URL:-http://${REMOTE_STACK_HOST}:18110/api/v1}" ;;
    billing-service) printf '%s' "http://${REMOTE_STACK_HOST}:18102/api/v1" ;;
    consultations-service) printf '%s' "${E2E_CONSULTATIONS_BASE_URL:-http://127.0.0.1:18021/api/v1}" ;;
    matter-service) printf '%s' "${E2E_MATTER_BASE_URL:-http://127.0.0.1:18020/api/v1}" ;;
    knowledge-service) printf '%s' "${KNOWLEDGE_SERVICE_URL:-http://${REMOTE_STACK_HOST}:18106/api/v1}" ;;
    files-service) printf '%s' "${E2E_FILES_BASE_URL:-http://${REMOTE_STACK_HOST}:18104/api/v1}" ;;
    platform-service) printf '%s' "http://${REMOTE_STACK_HOST}:18111/api/v1" ;;
    templates-service) printf '%s' "${E2E_TEMPLATES_BASE_URL:-http://${REMOTE_STACK_HOST}:18112/api/v1}" ;;
    collector-service) printf '%s' "http://${REMOTE_STACK_HOST}:18115/api/v1" ;;
    notification-service) printf '%s' "http://${REMOTE_STACK_HOST}:18109/api/v1" ;;
    memory-service) printf '%s' "${E2E_MEMORY_BASE_URL:-http://${REMOTE_STACK_HOST}:18108/api/v1}" ;;
    *) return 1 ;;
  esac
}

echo "=== LawSeekDog 服务健康检查 ==="
echo "Gateway: ${BASE_URL}"
echo ""

if [ "${E2E_USE_GATEWAY}" = "1" ]; then
  echo -n "Gateway: "
  if curl -s -o /dev/null -w "%{http_code}" "${GATEWAY_ROOT}/healthz" | grep -q "200"; then
    echo "✓ healthy"
  else
    echo "✗ unhealthy"
  fi

  for service in "${SERVICES[@]}"; do
    echo -n "${service}: "
    response=$(curl -s -H "X-Internal-Api-Key: ${INTERNAL_API_KEY}" -o /dev/null -w "%{http_code}" "${BASE_URL}/${service}/internal/actuator/health" 2>/dev/null || echo "000")
    if [ "$response" == "200" ]; then
      echo "✓ healthy"
    else
      echo "✗ unhealthy (${response})"
    fi
  done
else
  echo "Gateway: skipped (direct service mode)"
  for service in "${SERVICES[@]}"; do
    base="$(service_base_url "$service")"
    echo -n "${service}: "
    response=$(curl -s -H "X-Internal-Api-Key: ${INTERNAL_API_KEY}" -o /dev/null -w "%{http_code}" "${base}/internal/actuator/health" 2>/dev/null || echo "000")
    if [ "$response" == "200" ]; then
      echo "✓ healthy"
    else
      echo "✗ unhealthy (${response})"
    fi
  done
fi

echo ""
echo "=== 检查完成 ==="
