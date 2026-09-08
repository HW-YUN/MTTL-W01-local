# 네트워크

로컬 릴레이가 동작하려면 두 가지가 필요합니다.

1. 기기가 릴레이 호스트의 포트(`80` / `443` / `18831`)에 도달할 수 있을 것
2. 기기의 클라우드용 도메인 질의가 실제 서버 대신 릴레이로 향할 것 (DNS 리다이렉트)

그리고 한 가지를 권장합니다.

3. 기기가 있는 네트워크 세그먼트의 아웃바운드 인터넷 차단

이 문서의 예시에서 릴레이 호스트 IP 는 `<RELAY_HOST_IP>`, 기존 상위 DNS resolver 는
`<UPSTREAM_DNS_IP>` 로 표기합니다. 실제 값은 각자 환경에 맞게 채워 넣으세요. 실행
스크립트는 이 토큰을 기본값으로 쓰지 않습니다(환경 변수나 인자로 전달하지 않으면
사용법을 출력하고 종료합니다).

---

## 클라우드 아웃바운드 차단

- 기기 세그먼트의 아웃바운드 인터넷을 차단하는 것을 권장합니다.
- 릴레이는 decloud 모드로 동작하므로 어떤 요청도 외부로 전달하지 않습니다. 별도로
  "일부 클라우드 접속을 열어줘야" 하는 요구사항은 없습니다.
- `deploy/proxmox-lxc/preflight_check.sh` 는 기기 세그먼트에서 인터넷으로 나가는
  경로가 실제로 막혀 있는지 확인합니다.

## DNS 리다이렉트 (mef / brk2)

릴레이 스택은 `dnsmasq` 를 사용합니다. 예시 설정은
`config/dns/mttl-relay.conf.example` 에 있습니다.

```
address=/mef.onem2m.uplus.co.kr/<RELAY_HOST_IP>
address=/brk2.onem2m.uplus.co.kr/<RELAY_HOST_IP>
```

- 두 도메인만 릴레이로 향하게 합니다. 나머지 이름은 기존 상위 resolver
  (`<UPSTREAM_DNS_IP>`)로 정상 해석됩니다.
- `dev.toi.ommeq.com` 은 **일부러 리다이렉트하지 않습니다.**

### 적용

릴레이가 이미 떠 있고 기기 전원을 넣을 준비가 되었을 때 실행합니다(STOP-POINT
동작). 릴레이 호스트에서 root 로:

```sh
MTTL_RELAY_HOST_IP=<RELAY_HOST_IP> sh deploy/proxmox-lxc/dns/apply_dns_redirect.sh
```

또는 토큰을 직접 채운 설정 파일을 인자로 전달할 수도 있습니다.

```sh
sh deploy/proxmox-lxc/dns/apply_dns_redirect.sh /path/to/filled-in-mttl-relay.conf
```

둘 다 없으면 스크립트는 사용법을 출력하고 종료합니다. 설치 후에도 `<RELAY_HOST_IP>`
토큰이 남아 있으면 설치를 거부합니다. 스크립트는 적용 전에 기존
`/etc/dnsmasq.d/` 상태를 백업합니다.

### 확인

호스트 자체 resolver 가 로컬 dnsmasq 를 우회할 수 있으므로, 로컬 dnsmasq
(`127.0.0.1`)에 직접 질의해 확인합니다.

- `mef` / `brk2` → `<RELAY_HOST_IP>` 로 해석되어야 함
- `dev.toi.ommeq.com` → 변경되지 않아야 함

### 롤백

```sh
sh deploy/proxmox-lxc/dns/rollback_dns_redirect.sh
```

정확한 파일 이름 하나(`/etc/dnsmasq.d/mttl-relay.conf`)만 삭제하고 dnsmasq 를 다시
로드합니다. 삭제 전에 백업을 만듭니다. 이후 실제 상위 서버 값과 비교하려면
리다이렉트되지 않은 resolver 로 직접 조회하세요(예: `dig +short mef.onem2m.uplus.co.kr @1.1.1.1`).

## QMS sink DNS (선택)

기기의 진단 로그("QMS") HTTPS 업로드가 어디로 가는지 로컬에서 관찰하고 싶을 때만
적용합니다. 예시 설정은 `config/dns/mttl-qms-sink.conf.example` 입니다.

```
address=/log.toi.ommeq.com/<RELAY_HOST_IP>
address=/hdslog.lguplus.co.kr/<RELAY_HOST_IP>
```

### 적용 / 롤백

```sh
MTTL_RELAY_HOST_IP=<RELAY_HOST_IP> sh deploy/proxmox-lxc/dns/apply_qms_sink_dns.sh
sh deploy/proxmox-lxc/dns/rollback_qms_sink_dns.sh
```

- 이 스크립트는 `/etc/dnsmasq.d/mttl-qms-sink.conf` **한 개 파일만** 추가/삭제합니다.
  기존 `mttl-relay.conf`(mef / brk2 리다이렉트)는 절대 건드리지 않습니다.
- QMS 요청이 릴레이에 도달하면 `relay/rules/16-qms-sink.py` 가 관찰만 하고,
  아무것도 외부로 전달하지 않습니다. 자세한 내용은
  [firmware-ota.md](firmware-ota.md#qms) 를 참고하세요.

## 권장: 전용 resolver / 세그먼트

DNS 리다이렉트를 적용한 resolver 를 사용하는 **다른 기기에도 영향**이 갑니다.
MTTL-W01 전용 resolver 또는 전용 네트워크 세그먼트에서 적용하는 것을 권장합니다.
자세한 보안 배경은 [../SECURITY.md](../SECURITY.md) 를 참고하세요.

## 전체 롤백

DNS 리다이렉트 제거 + 서비스 중지를 한 번에 하려면:

```sh
sh deploy/proxmox-lxc/rollback_all.sh
```

기기 전원 차단과 공장 초기화는 이 스크립트가 하지 않으며, 필요한 수동 단계를
안내만 합니다. 문제 해결은 [troubleshooting.md](troubleshooting.md) 를 참고하세요.

---

[← README](../README.md)
