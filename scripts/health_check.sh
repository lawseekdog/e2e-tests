#!/bin/bash
# 健康检查脚本

set -e

BASE_URL="${BASE_URL:-http://localhost:18001/api/v1}"
INTERNAL_API_KEY="${INTERNAL_API_KEY:-change-me-in-production-32chars}"

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

echo "=== LawSeekDog 服务健康检查 ==="
echo "Gateway: ${BASE_URL}"
echo ""

# 检查 Gateway
echo -n "Gateway: "
if curl -s -o /dev/null -w "%{http_code}" "${GATEWAY_ROOT}/healthz" | grep -q "200"; then
  echo "✓ healthy"
else
  echo "✗ unhealthy"
fi

# 通过 Gateway 检查各服务（统一路由：/api/v1/{service-name}/**）
for service in "${SERVICES[@]}"; do
  echo -n "${service}: "
  response=$(curl -s -H "X-Internal-Api-Key: ${INTERNAL_API_KEY}" -o /dev/null -w "%{http_code}" "${BASE_URL}/${service}/internal/actuator/health" 2>/dev/null || echo "000")
  if [ "$response" == "200" ]; then
    echo "✓ healthy"
  else
    echo "✗ unhealthy (${response})"
  fi
done

echo ""
echo "=== 检查完成 ==="
