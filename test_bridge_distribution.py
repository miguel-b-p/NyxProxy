#!/usr/bin/env python3
"""
Script para testar a distribuição de requisições do Load Balancer.
Verifica se diferentes proxies estão sendo usadas em cada request.
"""

import requests
import time
from collections import Counter

LOAD_BALANCER_URL = "http://127.0.0.1:8080"
TEST_REQUESTS = 20

print("🧪 Testando Distribuição do Load Balancer")
print("=" * 50)
print(f"Load Balancer: {LOAD_BALANCER_URL}")
print(f"Número de requests: {TEST_REQUESTS}")
print("")

ips = []
print("Fazendo requisições...")

for i in range(1, TEST_REQUESTS + 1):
    try:
        response = requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": LOAD_BALANCER_URL, "https": LOAD_BALANCER_URL},
            timeout=10
        )
        ip = response.json()['ip']
        ips.append(ip)
        print(f"  Request {i:2d}: {ip}")
        time.sleep(0.3)
    except Exception as e:
        print(f"  Request {i:2d}: ❌ Error - {e}")

print("")
print("📊 Estatísticas de Distribuição:")
print("-" * 50)

if ips:
    ip_counts = Counter(ips)
    unique_ips = len(ip_counts)
    
    print(f"✅ Total de IPs únicos usados: {unique_ips}")
    print(f"✅ Total de requests bem-sucedidas: {len(ips)}")
    print("")
    print("Distribuição por IP:")
    for ip, count in ip_counts.most_common():
        percentage = (count / len(ips)) * 100
        bar = "█" * int(percentage / 2)
        print(f"  {ip:20s}: {count:2d} requests ({percentage:5.1f}%) {bar}")
    
    print("")
    if unique_ips >= 3:
        print("✅ SUCESSO! O load balancer está distribuindo entre múltiplas proxies!")
    elif unique_ips == 1:
        print("⚠️  AVISO: Apenas 1 IP detectado. Possíveis causas:")
        print("   - Apenas 1 bridge ativo")
        print("   - Todas as proxies têm o mesmo IP de saída")
    else:
        print("✅ OK! Distribuição funcionando.")
else:
    print("❌ Nenhuma requisição bem-sucedida!")

print("")
print("💡 Dica: Execute 'bridge stats' na interface do NyxProxy para mais detalhes")
