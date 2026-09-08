# 구조

이 프로젝트는 MTTL-W01 이 제조사 클라우드(Ommeq / Voltra / LG U+)로 향하던 연결을
로컬 릴레이로 받아, 클라우드 없이 기기를 동작시키고 Home Assistant 및 로컬
대시보드에 연결합니다.

## 전체 흐름

```
MTTL-W01
  │  (DNS 리다이렉트로 mef / brk2 가 릴레이를 가리킴)
  ▼
로컬 릴레이 (smartrelay, decloud 모드)
  ├─ MEF HTTPS 응답            :443
  ├─ MQTT / oneM2M            :18831
  ├─ 대시보드 피드 규칙        (상태를 devices.json 에 기록)
  ├─ OTA 규칙                  (단일 대상, fail-closed)
  └─ QMS sink 규칙             (진단 업로드를 로컬에서 관찰만)
       │  observer :9883 (평문 MQTT, localhost 전용)
       ├──────────────► 로컬 대시보드  :8080
       │
       ▼
  HA MQTT 브리지 (독립 프로세스)
       │
       ▼
  Home Assistant 의 MQTT 브로커 (Mosquitto)
       │
       ▼
  Home Assistant
```

## 구성 요소

### 로컬 릴레이 (`vendor/smartrelay/` + `relay/`)

smartrelay 를 기반으로 하며, `relay.py serve --config smartrelay.toml` 로 실행합니다.
설정 파일에 `dns =` 항목이 **없으면 decloud 모드**로 동작합니다. 이 모드에서 릴레이는
어떤 요청도 외부로 전달하지 않고, `rules/*.py` 로 HTTP / MQTT 응답을 로컬에서 직접
생성합니다.

- `relay/rules/99-default.py` — smartrelay 의 MTTL-W01 프로토콜 구현(MEF authdata,
  oneM2M bootstrap 응답, 텔레메트리 파싱)의 사본입니다. 실제 프로토콜 응답 내용이
  전부 여기에 있습니다.
- `relay/rules/10-dashboard-feed.py` — 관찰 전용 규칙. 모든 훅이 `None` 을 반환하므로
  실제 응답은 `99-default.py` 가 처리하고, 이 규칙은 본 것을 `state/devices.json` 에
  기록만 합니다.
- `relay/rules/15-firmware-ota.py` — 단일 대상 OTA 규칙. [firmware-ota.md](firmware-ota.md)
  참고.
- `relay/rules/16-qms-sink.py` — 진단 로그("QMS") HTTPS 업로드를 로컬에서 관찰만
  하고, 아무것도 전달하지 않습니다.

규칙은 파일 이름의 숫자 접두어 순서(`10` → `15` → `16` → `99`)로 로드됩니다.

### TLS 종단

릴레이는 도메인을 미리 몰라도 됩니다. TLS ClientHello 의 SNI 로 도메인을 알아내고,
해당 도메인용 leaf 인증서가 없으면 로컬 Root CA 로 즉석 발급·캐싱한 뒤 기기 쪽 TLS 를
종단합니다.

- `relay/gen_local_ca.sh` 가 Root CA(RSA-2048, 자체 서명)와 `mef` / `brk2` leaf 를
  생성합니다.
- **RSA 가 필수**입니다. 기기는 static-RSA(`TLS_RSA_*`) 계열 cipher 만 협상합니다.

### 로컬 대시보드 (`dashboard/`)

표준 라이브러리 `http.server` 기반이며, 프레임워크나 별도 정적 파일 없이 파이썬
소스 안의 인라인 HTML 문자열을 서빙합니다.

- `devices.json`(릴레이의 피드 규칙이 기록) 에서 텔레메트리 / 식별 정보를 읽고,
- 릴레이 observer 에 `{"list":true}` 로 물어 현재 MQTT 세션이 살아 있는지 확인합니다.

자세한 내용은 [dashboard.md](dashboard.md) 를 참고하세요.

### HA MQTT 브리지 (`bridge/`)

릴레이와 별개인 독립 프로세스입니다. smartrelay 프로토콜을 다시 구현하지 않고,

- 텔레메트리 / 콘센트 상태 → `devices.json`
- 온라인 여부 → 릴레이 observer 의 세션 목록
- 콘센트 명령 → 릴레이 observer 주입

을 사용해, Home Assistant 의 Mosquitto 브로커로 MQTT Discovery 를 통해 미러링합니다.
릴레이와는 약결합입니다(브로커 장애나 브리지 크래시가 릴레이·기기 로컬 제어에
영향을 주지 않습니다). 자세한 내용은 [home-assistant.md](home-assistant.md) 를
참고하세요.

## 포트

| 포트 | 프로토콜 | 용도 |
|---|---|---|
| `80` | HTTP (평문) | 기기가 `GET /mef/cert/http/pem` 으로 로컬 Root CA 를 받아감 |
| `443` | HTTPS | MEF / HTTP 응답 (SNI 기본값 `mef.onem2m.uplus.co.kr`) |
| `18831` | MQTT (TLS) | MQTT / oneM2M (펌웨어에서 확인된 브로커 포트, SNI 기본값 `brk2.onem2m.uplus.co.kr`) |
| `9883` | MQTT (평문) | observer — 대시보드 / 브리지의 tap · inject. **localhost 전용** |
| `8080` | HTTP | 로컬 대시보드 |

`18833` 은 레거시/미확인 포트입니다. 기본 설정에는 포함되지 않습니다. 실제 패킷
캡처에서 기기가 `:18833` 으로 SYN 을 보내는 것이 확인된 경우에만
`config/relay/smartrelay.toml.example` 에 포트 항목을 추가하세요. 롤백 스크립트는
안전을 위해 이 포트도 함께 확인합니다.

## 도메인

| 도메인 | 처리 |
|---|---|
| `mef.onem2m.uplus.co.kr` | 릴레이로 리다이렉트 (MEF / bootstrap) |
| `brk2.onem2m.uplus.co.kr` | 릴레이로 리다이렉트 (MQTT 브로커) |
| `dev.toi.ommeq.com` | **리다이렉트하지 않음** — 그대로 둠 |
| `log.toi.ommeq.com` | 선택적 QMS sink 대상 (`1.0.105` 의 QMS 엔드포인트) |
| `hdslog.lguplus.co.kr` | 선택적 QMS sink 대상 (`1.0.66` 실행 기록에서 관찰됨. 같은 서비스인지는 확인되지 않음) |

DNS 리다이렉트의 적용·롤백은 [network.md](network.md) 를 참고하세요.

## EKI / entityId

릴레이가 MEF authdata / oneM2M bootstrap 응답을 만들 때 사용하는 EKI / entityId
관련 구조는 분석 과정에서 별도로 검증했습니다. 릴레이는 이 값을 기기별로 자체
생성하며, 관련 상수는 이 저장소에 포함하지 않습니다.

---

[← README](../README.md)
