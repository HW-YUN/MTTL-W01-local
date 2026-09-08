# MTTL-W01 로컬 제어 스택

LG U+ **MTTL-W01** 스마트 멀티탭을 제조사 클라우드 없이 로컬(Proxmox VE 위의
Debian LXC + [smartrelay](https://github.com/3735943886/smartrelay))에서 제어하고,
Home Assistant 및 로컬 대시보드에 연결하는 개인 프로젝트입니다.

> 개인이 직접 구축해 사용 중인 환경을 정리해 공유하는 것입니다. 공식 지원 프로젝트가
> 아니며, LG U+ / TONLY / 제조사와 무관합니다. [DISCLAIMER.md](DISCLAIMER.md) 참고.

## 무엇이 가능한가

- 기기 세그먼트의 클라우드 아웃바운드를 차단한 상태에서 기기를 정상 동작시킴
- 로컬에서 콘센트 제어 / 전력·에너지·전압·온도 관찰
- Home Assistant 연동 (MQTT Discovery)
- 로컬 대시보드
- 펌웨어 OTA 와 진단 로그(QMS) 업로드를 로컬에서 처리

## 검증 환경

이 프로젝트는 **Proxmox VE 위의 Debian LXC** 에서 구현하고 실제 MTTL-W01 기기로
검증했습니다. 이 조합만 "검증됨" 으로 표시합니다.

- Docker 는 이 프로젝트에서 검증하지 않았습니다. 다른 Linux / Docker 환경에서도
  응용할 수 있겠지만 검증 대상이 아닙니다. Docker 중심 구성은
  [af950833/mttl_w01](https://github.com/af950833/mttl_w01) 을 참고하세요.

## 구조 요약

```
MTTL-W01 → (DNS 리다이렉트) → 로컬 릴레이(decloud)
                                 ├─ 로컬 대시보드
                                 └─ HA MQTT 브리지 → Mosquitto → Home Assistant
```

자세한 내용은 [docs/architecture.md](docs/architecture.md).

## 공개 기능

| 영역 | 내용 |
|---|---|
| 로컬 릴레이 | MEF HTTPS / MQTT / oneM2M 로컬 서빙, cloud egress 없음, strict MAC, sticky client_id→MAC |
| 대시보드 | 기기·온라인 상태, 콘센트 제어(기본 비활성), 전력·에너지·전압·내부 온도·펌웨어 |
| Home Assistant | MQTT Discovery, 기기당 10개 entity (v/t/p/e 텔레메트리를 보낸 `1.0.106` 계열은 20개) |
| OTA / QMS | 단일 대상 · fail-closed OTA, 진단 로그 업로드 로컬 관찰 |

## 빠른 시작

Proxmox 호스트에서 저장소를 받은 뒤:

```sh
sh deploy/proxmox-lxc/lxc_readiness_readonly.sh <CTID>     # 준비 상태 점검 (읽기 전용)
sh deploy/proxmox-lxc/deploy_to_lxc.sh <CTID>              # 배포 (서비스 미시작)
```

이후 CA 생성, 서비스 시작, DNS 리다이렉트 적용(STOP-POINT) 순서로 진행합니다.
전체 절차는 [docs/installation.md](docs/installation.md) 를 따르세요.

## Home Assistant 연동

HA 브리지는 릴레이와 별개인 독립 프로세스로, 로컬 릴레이가 보는 상태를 HA 의 MQTT
브로커에 미러링합니다. 스위치 상태는 실제 텔레메트리에서 오고, reconciliation 주기
등은 [docs/home-assistant.md](docs/home-assistant.md) 를 참고하세요.

## 로컬 대시보드

표준 라이브러리만으로 만든 로컬 전용 관찰·제어 화면입니다(`:8080`). 콘센트 제어는
기본적으로 비활성입니다. [docs/dashboard.md](docs/dashboard.md).

## 펌웨어 / OTA

되돌리기 어려운 위험 작업입니다. 본 프로젝트가 실기 검증한 펌웨어는 `1.0.66`(순정
참고)과 `1.0.106`(투오 님 커스텀)이며, 펌웨어 바이너리는 이 저장소에 포함하지
않습니다. 반드시 [docs/firmware-ota.md](docs/firmware-ota.md) 를 먼저 읽으세요.

## 네트워크 / 클라우드 격리

기기 세그먼트의 아웃바운드 인터넷 차단을 권장합니다. DNS 리다이렉트로
`mef` / `brk2` 만 릴레이로 향하게 하며, 적용·롤백 스크립트가 있습니다.
[docs/network.md](docs/network.md).

## smartrelay / 제3자 크레딧

이 저장소에 포함된 smartrelay 원본/수정본의 공개·재배포는 원작자에게 별도로
허락받았습니다(smartrelay 저장소 GitHub Issue #3). smartrelay 에는 별도의 오픈소스
라이선스가 부여되어 있지 않으므로, "MIT 라이선스" 등으로 표현하지 않습니다.
자세한 출처와 허락 범위는 [CREDITS.md](CREDITS.md) 를 참고하세요.

## 라이선스

이 프로젝트가 직접 작성한 코드·설정 예시·배포 스크립트·문서는 저장소 루트의
[LICENSE](LICENSE) (MIT) 조건을 따릅니다. 단 `vendor/smartrelay/**` 와
`relay/rules/99-default.py` 는 MIT 적용 대상이 아니며, 그 출처와 공개·재배포 조건은
[CREDITS.md](CREDITS.md) 를 따릅니다.

## 상세 문서

- [docs/installation.md](docs/installation.md) — 설치 (Proxmox LXC)
- [docs/architecture.md](docs/architecture.md) — 구조
- [docs/network.md](docs/network.md) — 네트워크 / DNS 리다이렉트
- [docs/home-assistant.md](docs/home-assistant.md) — Home Assistant 연동
- [docs/dashboard.md](docs/dashboard.md) — 로컬 대시보드
- [docs/firmware-ota.md](docs/firmware-ota.md) — 펌웨어 / OTA
- [docs/troubleshooting.md](docs/troubleshooting.md) — 문제 해결
- [docs/references.md](docs/references.md) — 참고 자료
- [CREDITS.md](CREDITS.md) — 사람 / 프로젝트 / 허락 출처
- [LICENSE](LICENSE) — 라이선스 (직접 작성한 부분에 적용되는 MIT + 적용 범위)
- [SECURITY.md](SECURITY.md) — 보안 안내
- [DISCLAIMER.md](DISCLAIMER.md) — 면책 안내

## 주의사항 / 면책

- 개인 프로젝트입니다. SLA·보증이 없고, 모든 환경 동작을 보장하지 않습니다.
- 펌웨어 / OTA / 네트워크 변경은 위험 작업이며 결과 책임은 사용자에게 있습니다.
- 민감정보(실제 MAC / client_id / IP / 시크릿 / 인증서)를 공개 이슈에 올리지
  마세요.

자세한 내용: [DISCLAIMER.md](DISCLAIMER.md), [SECURITY.md](SECURITY.md).

## 프로젝트 상태

개인적으로 실사용 중인 로컬화 환경을 정리해 공유합니다. `1.0.106` 커스텀 펌웨어를
사용한 구성을 실기 환경에서 검증했습니다.
