#!/usr/bin/env bash
# ============================================================
# 產生網頁後台用的自簽 HTTPS 憑證（有效 3 年）
#
# 用法：
#   scripts/gen_self_signed_cert.sh                 # 主機名稱用 `hostname`
#   scripts/gen_self_signed_cert.sh 192.168.20.106  # 指定 IP 或網域（可給多個）
#
# 產出 certs/admin.crt、certs/admin.key，再於 .env 設定：
#   ADMIN_SSL_CERT=certs/admin.crt
#   ADMIN_SSL_KEY=certs/admin.key
#
# 自簽憑證瀏覽器第一次會顯示「不安全」警告，確認後即可加密連線。
# 有公司 CA 簽發的憑證時直接換成那一組檔案即可。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p certs

NAMES=("$@")
[ ${#NAMES[@]} -eq 0 ] && NAMES=("$(hostname)")

SAN="DNS:localhost,IP:127.0.0.1"
for n in "${NAMES[@]}"; do
  if [[ "$n" =~ ^[0-9.]+$ ]]; then SAN="$SAN,IP:$n"; else SAN="$SAN,DNS:$n"; fi
done

openssl req -x509 -newkey rsa:2048 -nodes -days 1095 \
  -keyout certs/admin.key -out certs/admin.crt \
  -subj "/CN=${NAMES[0]}/O=IIoT SCADA" \
  -addext "subjectAltName=$SAN"

chmod 600 certs/admin.key
echo "✅ 已產生 certs/admin.crt、certs/admin.key（SAN: $SAN）"
