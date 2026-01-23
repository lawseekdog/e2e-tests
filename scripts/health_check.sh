#!/bin/bash
# 健康检查脚本

set -e

BASE_URL="${BASE_URL:-http://localhost:18001}"
INTERNAL_API_KEY="${INTERNAL_API_KEY:-change-me-in-production-32chars}"

SERVICES=(
  "auth-service"
  "user-service"
  "consultations-service"
  "matter-service"
  "knowledge-service"
  "files-service"
  "platform-service"
  "templates-service"
  "collector-service"
  "memory-service"
)

echo "=== LawSeekDog 服务健康检查 ==="
echo "Gateway: ${BASE_URL}"
echo ""

# 检查 Gateway
echo -n "Gateway: "
if curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/internal/actuator/health" | grep -q "200"; then
  echo "✓ healthy"
else
  echo "✗ unhealthy"
fi

# 通过 Gateway 检查各服务
for service in "${SERVICES[@]}"; do
  echo -n "${service}: "
  # Java 服务的 actuator 暴露在 /internal/actuator/health，并要求 internal api key
  response=$(curl -s -H "X-Internal-Api-Key: ${INTERNAL_API_KEY}" -o /dev/null -w "%{http_code}" "${BASE_URL}/internal/${service}/internal/actuator/health" 2>/dev/null || echo "000")
  if [ "$response" == "200" ]; then
    echo "✓ healthy"
  else
    echo "✗ unhealthy (${response})"
  fi
done

echo ""
echo "=== 检查完成 ==="
