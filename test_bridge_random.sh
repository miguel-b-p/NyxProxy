#!/bin/bash

# Script para testar se o load balancer está usando proxies diferentes
# em cada requisição no modo random

echo "🧪 Testando Load Balancer - Modo Random"
echo "========================================"
echo ""
echo "Fazendo 10 requisições através do load balancer..."
echo "Se estiver funcionando corretamente, você verá IPs diferentes:"
echo ""

for i in {1..10}; do
    echo -n "Request $i: "
    curl -s --proxy http://127.0.0.1:8080 https://api.ipify.org?format=json | jq -r '.ip'
    sleep 0.5
done

echo ""
echo "✅ Se você viu vários IPs diferentes, o load balancer está funcionando!"
echo "📊 Execute 'bridge stats' na interface para ver a distribuição"
