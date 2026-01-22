#!/bin/bash
# 健康检查脚本

set -e

BASE_URL="${BASE_URL:-http://localhost:18001}"

SERVICES=(
  "auth-service"
  "user-service"
  "consultations-service"
  "matter-service"
  "knowledge-service"
  "files-service"
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
  # 尝试访问服务的健康检查端点
  response=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/internal/${service}/actuator/health" 2>/dev/null || echo "000")
  if [ "$response" == "200" ]; then
    echo "✓ healthy"
  else
    echo "✗ unhealthy (${response})"
  fi
done

echo ""
echo "=== 检查完成 ==="
