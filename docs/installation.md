# 설치

이 프로젝트가 실제로 검증된 환경은 **Proxmox VE 위의 Debian LXC 컨테이너** 하나뿐
입니다. 이 문서는 그 경로를 기준으로 설명합니다. 다른 Linux / Docker 환경에서도
응용할 수 있겠지만 검증 대상이 아니며, Docker 중심 구성은
[af950833/mttl_w01](https://github.com/af950833/mttl_w01) 을 참고하세요.

전체 구조를 먼저 이해하려면 [architecture.md](architecture.md) 를, DNS·네트워크
배경은 [network.md](network.md) 를 참고하세요.

예시의 `<CTID>` 는 LXC 컨테이너 번호, `<RELAY_HOST_IP>` 는 릴레이(=이 컨테이너)의
IP 입니다. 실제 값으로 바꿔 넣으세요.

---

## 1. 사전 요구사항

- Proxmox VE 호스트 (`pct` 명령 사용 가능)
- Debian LXC 컨테이너 하나
  - **Python 3.11 이상** (릴레이가 `tomllib` 를 사용합니다)
  - `openssl` (로컬 CA 생성)
  - `dnsmasq` (DNS 리다이렉트)
  - 컨테이너 안에서 TCP `80` / `443` / `18831` 바인딩 가능
  - 본 프로젝트에서 검증한 Proxmox VE 9.2.6 + Debian 12 unprivileged LXC 구성에서는
    `nesting=1` 을 사용했습니다. `nesting` 없이 생성한 검증 LXC 에서는 systemd 가
    `degraded` 상태(`systemd-networkd` / `systemd-logind` 실패)였고, `nesting=1`
    적용 후 정상 `running` 상태를 확인했습니다. 다른 Proxmox / LXC 조합에서는 필요
    여부가 다를 수 있습니다.
- 기기와 릴레이가 같은 네트워크 세그먼트에 있고, 기기가 릴레이 IP 에 도달 가능

## 2. 디렉터리 배치

배포 스크립트는 저장소 트리를 컨테이너의 `/srv/mttl-lab/` 아래로 밀어 넣습니다.

```
/srv/mttl-lab/
├── relay/            relay.py + 의존 모듈 + smartrelay.toml + rules/ + certs/ + state/
├── dashboard/
├── bridge/
├── vendor/smartrelay/
├── config/
├── deploy/proxmox-lxc/
└── tests/
```

- 릴레이 상태 파일: `/srv/mttl-lab/relay/state/devices.json`
- 로컬 인증서: `/srv/mttl-lab/relay/certs/`
- HA 브리지 환경 파일: `/etc/mttl/ha-bridge.env` (7번 항목 참고)

## 3. LXC 준비 상태 점검 (읽기 전용)

Proxmox 호스트에서 저장소를 받은 뒤:

```sh
sh deploy/proxmox-lxc/lxc_readiness_readonly.sh <CTID> \
    2>&1 | tee lxc_readiness_$(date +%Y%m%d_%H%M%S).txt
```

- `<CTID>` 는 필수 위치 인자입니다(숫자만). 생략하면 사용법을 출력하고 종료합니다.
- 이 스크립트는 **아무것도 변경하지 않습니다.** 컨테이너의 상태·포트·dnsmasq·
  resolver·방화벽·용량·도구 버전을 조회만 합니다.

## 4. 배포

Proxmox 호스트에서(호스트에 `pct` 가 있어야 합니다):

```sh
# 밀어넣기 + 검증만, 서비스는 시작하지 않음
sh deploy/proxmox-lxc/deploy_to_lxc.sh <CTID>

# CA 생성 + relay / dashboard 서비스까지 시작
sh deploy/proxmox-lxc/deploy_to_lxc.sh <CTID> --start
```

`<CTID>` 대신 환경 변수로도 지정할 수 있습니다.

```sh
MTTL_LXC_CTID=<CTID> sh deploy/proxmox-lxc/deploy_to_lxc.sh
```

명시적 인자가 우선이고, 없으면 `MTTL_LXC_CTID` 를 사용하며, 둘 다 없으면 사용법을
출력하고 종료합니다. `--start` 는 위치와 무관한 옵션입니다.

이 스크립트가 하는 일:

1. 컨테이너에 기존 `/srv/mttl-lab/` 이 있으면 백업
2. 저장소 트리를 `/srv/mttl-lab/` 로 밀어넣기
3. `relay.py` 와 의존 모듈(`mqtt_session.py`, `mqtt_wire.py`, `rules_engine.py`)을
   `/srv/mttl-lab/relay/` 로 복사하고, `smartrelay.toml` 이 없으면
   `config/relay/smartrelay.toml.example` 을 복사
4. `mttl-relay.service`, `mttl-dashboard.service` 를 설치(활성화하지 않음)
5. 컨테이너 안에서 `tests/synthetic_tests.sh` 실행
6. `--start` 인 경우에만: `relay/gen_local_ca.sh` 로 CA 생성 + `mttl-relay`,
   `mttl-dashboard` 시작

> `deploy_to_lxc.sh` 는 **relay 와 dashboard 만** 다룹니다. HA 브리지
> (`mttl-ha-bridge.service`)는 아래 별도 단계로 설치합니다.

## 5. 로컬 CA

`--start` 없이 배포했다면 서비스를 시작하기 전에 CA 를 만들어야 합니다. 컨테이너
안에서:

```sh
cd /srv/mttl-lab
sh relay/gen_local_ca.sh /srv/mttl-lab/relay/certs
```

- 인자를 생략하면 스크립트 위치 기준 `relay/certs` 를 사용합니다.
- Root CA(RSA-2048)와 `mef` / `brk2` leaf 를 생성합니다. 개인키는 권한 `600` 이며
  **이 호스트에만** 두어야 합니다([../SECURITY.md](../SECURITY.md)).

## 6. 서비스 시작

컨테이너 안에서 root 로:

```sh
cd /srv/mttl-lab

# relay + dashboard (콘센트 제어 비활성)
sh deploy/proxmox-lxc/start_local_services.sh

# dashboard 를 콘센트 제어 활성 상태로 시작
sh deploy/proxmox-lxc/start_local_services.sh --control
```

- CA 가 없으면 먼저 생성한 뒤 `mttl-relay`, `mttl-dashboard` 를 재시작합니다.
- `--control` 은 대시보드 서비스에 drop-in 을 추가해 `--enable-control` 로 실행되게
  합니다. 위험 요소는 [../SECURITY.md](../SECURITY.md) 와 [dashboard.md](dashboard.md)
  를 참고하세요.

systemd 유닛은 기본적으로 활성화되어 있지 않습니다. 수동으로:

```sh
systemctl start mttl-relay
systemctl start mttl-dashboard
journalctl -u mttl-relay -f
```

## 7. HA 브리지 (선택)

Home Assistant 에 연결하려면 브리지를 따로 설정합니다.

1. 권한 없는 전용 사용자를 만듭니다. `mttl-ha-bridge.service` 는 `User=mttl` /
   `Group=mttl` 로 실행되므로 이 사용자가 있어야 합니다. 표준 방법의 예:

   ```sh
   useradd --system --no-create-home --shell /usr/sbin/nologin mttl
   ```

   (`StateDirectory=mttl-ha-bridge` 덕분에 `/var/lib/mttl-ha-bridge` 는 systemd 가
   이 사용자 소유로 자동 생성합니다.)

2. 환경 파일을 만듭니다.

   ```sh
   mkdir -p /etc/mttl
   cp /srv/mttl-lab/bridge/ha-bridge.env.example /etc/mttl/ha-bridge.env
   chmod 600 /etc/mttl/ha-bridge.env
   $EDITOR /etc/mttl/ha-bridge.env      # HA 브로커 값 채우기
   ```

3. 유닛을 설치하고 시작합니다.

   ```sh
   cp /srv/mttl-lab/deploy/proxmox-lxc/systemd/mttl-ha-bridge.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl start mttl-ha-bridge
   ```

HA 브로커 값(`MTTL_HA_MQTT_HOST` 등)이 비어 있으면 브리지는 오류만 로그로 남기고
유휴 상태로 있습니다(파괴적인 동작을 하지 않습니다). entity 구성과 reconciliation
설정은 [home-assistant.md](home-assistant.md) 를 참고하세요.

## 8. STOP-POINT — DNS 리다이렉트 + 기기 전원

여기서부터는 되돌리기가 번거로운 지점입니다. 릴레이가 떠 있는 상태에서:

```sh
cd /srv/mttl-lab

# 사전 점검 (읽기 전용)
sh deploy/proxmox-lxc/preflight_check.sh

# DNS 리다이렉트 적용
MTTL_RELAY_HOST_IP=<RELAY_HOST_IP> sh deploy/proxmox-lxc/dns/apply_dns_redirect.sh
```

그런 다음 기기 전원을 넣고 `journalctl -u mttl-relay -f` 와 대시보드
(`http://<RELAY_HOST_IP>:8080/`)를 함께 지켜봅니다. DNS 세부 내용은
[network.md](network.md) 를 참고하세요.

## 9. 확인

- 대시보드: `http://<RELAY_HOST_IP>:8080/`
- 릴레이 로그: `journalctl -u mttl-relay -f`
- 리스너: `ss -tlnp` 로 `80` / `443` / `18831` / `8080` 확인

문제가 있으면 [troubleshooting.md](troubleshooting.md) 를 참고하세요.

## 10. 롤백

```sh
cd /srv/mttl-lab
sh deploy/proxmox-lxc/rollback_all.sh
```

DNS 리다이렉트를 제거하고 `mttl-relay` / `mttl-dashboard` 를 중지·비활성화한 뒤
포트가 해제됐는지 확인하고, 남은 수동 단계(기기 전원 차단, 필요 시 공장 초기화)를
안내합니다. 공장 초기화 범위는 기기마다 다르므로 자신의 기기에서 직접 확인하세요.

---

[← README](../README.md)
