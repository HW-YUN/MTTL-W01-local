# 참고 자료

기술적으로 참고한 프로젝트·자료 목록입니다. 사람·허락 출처는
[CREDITS.md](../CREDITS.md) 를 참고하세요.

---

## upstream 프로젝트

| 프로젝트 | 링크 | 이 프로젝트에서의 위치 |
|---|---|---|
| smartrelay (3735943886) | <https://github.com/3735943886/smartrelay> | 로컬 릴레이(MEF / MQTT / oneM2M 로컬 서빙)의 기술 기반. `vendor/smartrelay/` 에 원본 일부 + 로컬 패치본 포함 |
| mttl_w01 (af950833) | <https://github.com/af950833/mttl_w01> | Docker 기반 로컬 서버 대안. `1.0.106` 커스텀 펌웨어 공개 경유 |

## 접근 방식 비교

| 방식 | 핵심 | 이 프로젝트와의 관계 |
|---|---|---|
| smartrelay (3735943886) | MEF / MQTT / oneM2M 기반 프록시 / decloud | 본 프로젝트가 사용하는 기술 기반 |
| mttl_w01 (af950833) | Docker 로컬 서버 + `1.0.106` 커스텀 펌웨어 공개 경유 | Docker 중심 구성 대안 |
| 순정 FW TCP 10086 / Matter Bridge (탱즈, [ttaengz](https://github.com/ttaengz/mttl-w01-matterbridge)) | 순정 펌웨어를 유지한 채 TCP 10086 으로 직접 제어(polling 기반 상태 갱신). 이후 SmartThings 환경을 위해 별도 Matter Bridge 서버 방식으로 확장 | 본 프로젝트와 다른 접근 |
| 투오(tuoway) Standalone | 커스텀 펌웨어(`1.0.110`) + TCP 10086 기반 Standalone / 쉬운 설치 구성. 탱즈님의 TCP 10086 접근을 이어받아 event push 등으로 polling 방식의 한계를 개선한 흐름이며, `1.0.106` 계보의 후속입니다(`1.0.106` 과는 다른 버전이지만 무관한 별개 계열은 아님) | 설치 편의형 후속 접근 |
| **본 프로젝트** | smartrelay + Proxmox LXC + HA MQTT Bridge (`1.0.66` stock / `1.0.106` custom 으로 검증) | — |

## 펌웨어

펌웨어 계보와 각 버전의 출처는 [firmware-ota.md](firmware-ota.md) 에서 다룹니다.
본 저장소는 펌웨어 바이너리를 재배포하지 않습니다.

## Home Assistant

- Home Assistant MQTT 통합 (MQTT Discovery) — 공식 문서

본 프로젝트의 HA 브리지는 Home Assistant 의 표준 MQTT Discovery 규약을 따릅니다.
자세한 내용은 [home-assistant.md](home-assistant.md) 를 참고하세요.

## 커뮤니티 게시물

MTTL-W01 로컬화는 네이버 카페와 Home Assistant 관련 커뮤니티에서 여러 사용자가
정보를 공유하면서 정리되어 왔습니다. 개별 게시물의 공개 링크가 확인되는 대로 이
목록에 추가할 예정입니다. 확인되지 않은 링크는 임의로 만들지 않습니다.

> 이 저장소는 커뮤니티에서 공유된 PDF·아카이브 파일을 재배포하지 않습니다.

---

[← README](../README.md)
