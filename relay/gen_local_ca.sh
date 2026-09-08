#!/bin/sh
# MTTL-W01 local relay — Root CA + Server leaves (mef / brk2).
#
# Extensions match vendor/smartrelay/relay.py exactly so the relay reuses
# these files from its cert cache (certs/ca.{crt,key}, certs/leaves/<domain>.{crt,key,chain.pem}).
#
#   Root CA : RSA-2048, self-signed, basicConstraints=critical,CA:TRUE,
#             keyUsage=critical,digitalSignature,keyCertSign,cRLSign
#   Leaf    : RSA-2048, CA:FALSE, keyUsage=critical,digitalSignature,keyEncipherment,
#             extendedKeyUsage=serverAuth, subjectAltName=DNS:<one domain>, signed by the Root CA
#             (RSA is MANDATORY — the device only negotiates static-RSA TLS_RSA_* ciphers)
#
# Private keys are written under CERT_DIR and MUST stay on the relay host only
# (repo .gitignore excludes *.key and certs/). Nothing here is committed.
#
# Usage:  sh relay/gen_local_ca.sh [CERT_DIR]
#         CERT_DIR default = relay/certs
set -eu

CERT_DIR="${1:-$(cd "$(dirname "$0")" && pwd)/certs}"
DAYS_CA=7300
DAYS_LEAF=7300
DOMAINS="mef.onem2m.uplus.co.kr brk2.onem2m.uplus.co.kr"

mkdir -p "$CERT_DIR/leaves"
CA_CRT="$CERT_DIR/ca.crt"
CA_KEY="$CERT_DIR/ca.key"
TD="$(mktemp -d)"
trap 'rm -rf "$TD"' EXIT

# ---- Root CA -------------------------------------------------------------
if [ -f "$CA_CRT" ] && [ -f "$CA_KEY" ]; then
  echo "[ca] reuse existing $CA_CRT"
else
  echo "[ca] new Root CA -> $CA_CRT"
  cat > "$TD/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
C = KR
O = Local MITM CA
CN = Local MITM Root CA
[ext]
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF
  openssl genrsa -out "$CA_KEY" 2048
  openssl req -new -x509 -key "$CA_KEY" -out "$CA_CRT" -days "$DAYS_CA" -config "$TD/ca.cnf"
  chmod 600 "$CA_KEY"
fi

# ---- Leaves -----------------------------------------------------------------
for DOMAIN in $DOMAINS; do
  L_CRT="$CERT_DIR/leaves/$DOMAIN.crt"
  L_KEY="$CERT_DIR/leaves/$DOMAIN.key"
  L_CHAIN="$CERT_DIR/leaves/$DOMAIN.chain.pem"
  if [ -f "$L_CHAIN" ] && [ -f "$L_KEY" ]; then
    echo "[leaf] reuse $L_CHAIN"
    continue
  fi
  echo "[leaf] new leaf for $DOMAIN"
  cat > "$TD/leaf.cnf" <<EOF
[req]
distinguished_name = dn
req_extensions = ext
prompt = no
[dn]
CN = $DOMAIN
[ext]
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @san
[san]
DNS.1 = $DOMAIN
EOF
  openssl genrsa -out "$L_KEY" 2048
  openssl req -new -key "$L_KEY" -out "$TD/leaf.csr" -config "$TD/leaf.cnf"
  openssl x509 -req -in "$TD/leaf.csr" -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -out "$L_CRT" -days "$DAYS_LEAF" -extfile "$TD/leaf.cnf" -extensions ext
  cat "$L_CRT" "$CA_CRT" > "$L_CHAIN"
  chmod 600 "$L_KEY"
done

# ---- Verify ---------------------------------------------------------------
echo
echo "==================== VERIFY ===================="
echo "--- Root CA ($CA_CRT) ---"
openssl x509 -in "$CA_CRT" -noout -subject -issuer -dates
openssl x509 -in "$CA_CRT" -noout -text | grep -E "CA:TRUE|Key Usage|Public.Key Algorithm|RSA Public-Key|Signature Algorithm" | sed 's/^/    /'
for DOMAIN in $DOMAINS; do
  L_CRT="$CERT_DIR/leaves/$DOMAIN.crt"
  echo "--- Leaf: $DOMAIN ---"
  openssl x509 -in "$L_CRT" -noout -subject -issuer
  openssl x509 -in "$L_CRT" -noout -text | grep -E "CA:FALSE|DNS:|TLS Web Server|Digital Signature|Key Encipherment|RSA Public-Key|Public.Key Algorithm" | sed 's/^/    /'
  echo "    chain verify:"
  openssl verify -CAfile "$CA_CRT" "$L_CRT" | sed 's/^/      /'
done
echo "==============================================="
echo "cert dir: $CERT_DIR   (keys are chmod 600, keep on this host only)"
