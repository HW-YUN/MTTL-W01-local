# 로컬 대시보드

`dashboard/mttl_dashboard.py` 는 표준 라이브러리 `http.server` 만으로 만든 로컬 전용
관찰·제어 화면입니다. 프레임워크나 별도 정적 파일이 없으며, 페이지 전체(HTML / CSS /
JS)가 파이썬 소스 안의 인라인 문자열로 서빙됩니다.

기본 포트는 `8080` 입니다: `http://<RELAY_HOST_IP>:8080/`

---

## 데이터 출처

| 표시 항목 | 출처 |
|---|---|
| 텔레메트리 / 식별 정보 (client_id, MAC, 전력, 온도 등) | 릴레이의 피드 규칙이 기록하는 `relay/state/devices.json` |
| 온라인 여부 (지금 MQTT 세션이 살아 있는가) | 릴레이 observer 에 `{"list":true}` 로 조회 |

피드 규칙은 MQTT 연결 종료를 볼 수 없으므로, 실시간 온라인 판정은 observer 조회에
의존합니다.

## 실행

```sh
python3 dashboard/mttl_dashboard.py \
  --host 0.0.0.0 --port 8080 \
  --state-file /srv/mttl-lab/relay/state/devices.json \
  --observer 127.0.0.1:9883 \
  --refresh 3
```

주요 옵션:

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--host` | `MTTL_DASHBOARD_HOST` 또는 `0.0.0.0` | 바인딩 호스트 |
| `--port` | `MTTL_DASHBOARD_PORT` 또는 `8080` | 포트 |
| `--state-file` | `MTTL_STATE_FILE` 또는 `/srv/mttl-lab/relay/state/devices.json` | 상태 파일 |
| `--observer` | `MTTL_OBSERVER` 또는 `127.0.0.1:9883` | 릴레이 observer (평문 MQTT) |
| `--refresh` | `3` | 페이지가 `/api/state` 를 폴링하는 주기(초) |
| `--enable-control` | (없음) | `POST /api/control` 로 콘센트 명령을 observer 에 publish 하도록 허용 |
| `--self-check` | (없음) | 설정·템플릿만 검증하고 종료 |

systemd 로 실행되는 경우 유닛은 기본적으로 `--enable-control` 없이 시작됩니다.

## HTTP 엔드포인트

| 경로 | 설명 |
|---|---|
| `GET /` | 대시보드 HTML. `--refresh` 주기로 `/api/state` 를 폴링 |
| `GET /api/state` | JSON — 서버 시각, 피드 갱신 시각, 기기별 상태, observer 상태 |
| `GET /api/live` | JSON — 릴레이의 현재 MQTT 세션 목록 (디버그용) |
| `GET /healthz` | `ok` |
| `POST /api/control` | `--enable-control` 일 때만. `{"key":..., "outlet":0..4, "on":true|false}` 또는 `{"key":..., "action":"status"}` |

## 화면 구성

상단에 기기 수 / 온라인 수 / 총 전력 / observer 상태(live · stale · down) / 제어
활성 여부가 표시됩니다.

기기마다 카드 하나가 있고, 카드는 세 그룹으로 나뉩니다.

### outlets

- `ALL ON` / `ALL OFF` 버튼
- 콘센트 1~4 각각: ON/OFF 상태, 전력(W), 누적 에너지(kWh), 내부 온도(°C), 그리고
  대기(standby) / 경보(overheat·overload) / 차단 임계값 설정이 있으면 함께 표시

### power

| 행 | 내용 |
|---|---|
| total power | 총 전력 (W) |
| energy total | 누적 에너지 (원시값) |
| voltage | 전압 (V). 유선 데이터에 전압이 없으면 `unknown` |

> 이 그룹에는 **voltage 행이 있고, current(전류) 행은 없습니다.** 전류는 유선
> 데이터에 실려 오지 않습니다.

### diagnostics

MQTT 세션 상태(live / none)와 세션 지속 시간, 재연결 횟수, MEF bootstrap 관찰 여부,
IP, client id, MAC, 모델, 펌웨어, 마지막 텔레메트리·마지막 관측 시각, 마지막 토픽,
Wi-Fi SSID / 신호 세기가 표시됩니다.

- **펌웨어** 행은 기기가 전압·온도·전력·에너지(v/t/p/e) 텔레메트리를 보낸 적이
  있으면 `1.0.106`, 아니면 `unknown` 으로 표시됩니다.

## 콘센트 제어

- 기본적으로 **비활성**입니다(systemd 유닛과 코드 양쪽 모두).
- `--enable-control` 일 때만 `on` / `off` / `ALL ON` / `ALL OFF` 버튼이 동작하며,
  명령은 릴레이 observer 로 publish 됩니다.
- 콘센트 on/off 는 이후 텔레메트리로 상태가 확인됩니다. `action: "status"`
  (STATUS_GET)는 fire-and-forget 이며 응답은 비동기로 옵니다.
- 활성화 전 주의사항은 [../SECURITY.md](../SECURITY.md) 를 참고하세요.

## 대시보드 상태 폴링 vs HA 브리지 reconciliation

대시보드는 자체적으로 주기적인 STATUS_GET 을 보낼 수 있습니다.

- 환경 변수 `MTTL_STATUS_INTERVAL`, **코드 기본값 `15`(초)**
- HA 브리지가 주기적 reconciliation 을 담당하는 경우(권장 배포), 대시보드 쪽은
  `MTTL_STATUS_INTERVAL=0` 으로 꺼서 두 곳에서 동시에 폴링하지 않도록 합니다.

이는 HA 브리지의 `MTTL_HA_STATUS_INTERVAL`(코드 기본값 `0` = OFF)과는 **별개
설정**입니다. [home-assistant.md](home-assistant.md#reconciliation) 을 참고하세요.

---

[← README](../README.md)
