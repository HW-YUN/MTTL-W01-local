# 문제 해결

각 항목은 **증상 → 확인 → 조치** 순서입니다. 여기 나온 확인 항목은 공개된
스크립트로 재현할 수 있는 것들입니다.

먼저 사전 점검을 돌려보면 대부분 여기서 걸립니다.

```sh
cd /srv/mttl-lab
sh deploy/proxmox-lxc/preflight_check.sh
```

---

## 릴레이가 시작되지 않는다

- **확인**
  - `python3 -V` 가 3.11 이상인가? (릴레이는 `tomllib` 를 사용합니다)
  - `openssl` 이 설치되어 있는가?
  - `/srv/mttl-lab/relay/` 에 `relay.py`, `smartrelay.toml`,
    `rules/99-default.py` 가 있는가?
  - `journalctl -u mttl-relay -n 20 --no-pager`
- **조치**
  - 배포가 누락됐으면 `sh deploy/proxmox-lxc/deploy_to_lxc.sh <CTID>` 를 다시 실행.
  - `smartrelay.toml` 이 없으면 `config/relay/smartrelay.toml.example` 을
    `relay/smartrelay.toml` 로 복사.

## 릴레이가 인증서/규칙을 못 찾는다 (WorkingDirectory)

- **확인**
  - `smartrelay.toml` 의 `cert_dir` / `rules_dir` 는 실행 디렉터리 기준 상대
    경로입니다.
  - systemd 유닛의 `WorkingDirectory` 가 `/srv/mttl-lab/relay` 인가?
- **조치**
  - 수동 실행 시 `cd /srv/mttl-lab/relay` 후 `python3 relay.py serve --config smartrelay.toml`.

## DNS 리다이렉트가 적용되지 않는다

- **확인**
  - 로컬 dnsmasq 에 직접 질의: `dig +short mef.onem2m.uplus.co.kr @127.0.0.1`
    (호스트 자체 resolver 는 dnsmasq 를 우회할 수 있습니다)
  - `dnsmasq --test`
  - `apply_dns_redirect.sh` 실행 시 `<RELAY_HOST_IP>` 토큰이 남아 있으면 설치가
    거부됩니다. `MTTL_RELAY_HOST_IP` 를 지정했는지 확인.
- **조치**
  - `MTTL_RELAY_HOST_IP=<RELAY_HOST_IP> sh deploy/proxmox-lxc/dns/apply_dns_redirect.sh`
  - 자세한 내용은 [network.md](network.md).

## 기기가 여전히 클라우드에 붙는다

- **확인**
  - 기기 세그먼트에서 인터넷으로 나가는 경로가 막혀 있는가? (`ping 1.1.1.1` 이
    실패해야 정상)
  - `mef` / `brk2` 가 릴레이로 해석되는가?
  - `relay/certs/ca.crt` 와 leaf 인증서가 있는가?
- **조치**
  - 아웃바운드 차단을 켜고, CA 가 없으면 `sh relay/gen_local_ca.sh /srv/mttl-lab/relay/certs`.

## Home Assistant 에 기기가 안 나타난다

- **확인**
  - `/etc/mttl/ha-bridge.env` 의 `MTTL_HA_MQTT_HOST` / `_USERNAME` / `_PASSWORD` 가
    채워져 있는가? (비어 있으면 브리지는 유휴 상태로 아무것도 발행하지 않습니다)
  - `systemctl is-active mttl-ha-bridge`
  - 해당 기기가 MAC 을 한 번이라도 제시했는가? (MAC 이 없으면 브리지는 discovery
    를 보류합니다)
- **조치**
  - 환경 파일을 채우고 `systemctl restart mttl-ha-bridge`.
  - [home-assistant.md](home-assistant.md).

## HA entity 신원이 바뀌었다 / 중복된다 (sticky MAC)

- **확인**
  - slug 는 MAC 기준입니다. IP 는 쓰이지 않습니다.
  - sticky 매핑 파일: `/var/lib/mttl-ha-bridge/slugmap.json` (`mttl` 소유,
    권한 `0700`).
  - 브리지 로그에 "conflicting MAC" 경고가 있는가? (write-once 이므로 다른 MAC 은
    자동 반영되지 않습니다)
- **조치**
  - 의도적으로 재바인딩하려면 매핑 파일을 직접 수정/삭제한 뒤 브리지를 재시작.

## 상태가 오래되어 보인다 (stale)

- **확인**
  - reconciliation 을 어디서 담당하는가? 브리지(`MTTL_HA_STATUS_INTERVAL`)와
    대시보드(`MTTL_STATUS_INTERVAL`) 중 한 곳만 폴링해야 합니다.
  - 코드 기본값: 브리지 `0`(OFF), 대시보드 `15`.
- **조치**
  - 브리지가 담당하면 `MTTL_HA_STATUS_INTERVAL=30`, 대시보드는 `MTTL_STATUS_INTERVAL=0`.

## 대시보드에 기기가 하나도 없다

- **확인**
  - decloud 모드에서는 정상입니다. mef / brk2 DNS 리다이렉트가 적용되고 기기
    전원이 들어와야 첫 요청이 들어옵니다.
  - 대시보드 상단의 observer 상태가 `down` 이 아닌가?
- **조치**
  - DNS 리다이렉트 적용 + 기기 전원. [installation.md](installation.md#8-stop-point--dns-리다이렉트--기기-전원).

## QMS 로그를 보고 싶다

- **확인**
  - `relay/state/qms-sink.log` (JSON Lines). 소스 IP · 시각 · 호스트 · 메서드 ·
    경로 · `Content-Length` · 일부 표식만 기록됩니다.
  - 진단 페이로드 전체는 기록되지 않으며, 어디로도 전달되지 않습니다.
- **조치**
  - QMS 호스트가 릴레이로 오지 않으면 QMS sink DNS 를 적용
    ([network.md](network.md#qms-sink-dns-선택)).

## OTA 가 아무 반응이 없다 (fail-closed)

- **확인**
  - `MTTL_OTA_TARGET_IP` 와 `MTTL_OTA_TARGET_ORIGIN` 이 설정되어 있는가? 없으면
    모든 요청이 차단됩니다.
  - `firmware-ota.state` 가 `SPENT` 인가? `SPENT` 는 종료 상태이며 재시작해도
    유지됩니다(ARM 파일이 치워집니다).
  - ARM 파일의 `sha256` 이 실제 `.fwr` 파일의 해시와 일치하는가?
- **조치**
  - 대상·ARM 을 정확히 설정. [firmware-ota.md](firmware-ota.md).

## LXC 준비 상태를 다시 보고 싶다

Proxmox 호스트에서:

```sh
sh deploy/proxmox-lxc/lxc_readiness_readonly.sh <CTID>
```

포트 바인딩 가능 여부, dnsmasq, resolver, 방화벽, 용량, 도구 버전을 조회만 합니다
(변경 없음).

## 전부 되돌리고 싶다

```sh
cd /srv/mttl-lab
sh deploy/proxmox-lxc/rollback_all.sh
```

DNS 리다이렉트 제거 + 서비스 중지·비활성화 + 포트 해제 확인. 기기 전원 차단과
공장 초기화는 수동 안내만 합니다.

---

[← README](../README.md)
