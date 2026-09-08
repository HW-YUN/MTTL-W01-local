# Home Assistant 연동

`bridge/mttl_ha_bridge.py` 는 릴레이와 별개인 독립 프로세스로, 로컬 릴레이가 보는
기기 상태를 Home Assistant 의 MQTT 브로커(Mosquitto)에 MQTT Discovery 로
미러링합니다.

설치는 [installation.md](installation.md#7-ha-브리지-선택) 의 브리지 항목을,
표준 규약은 Home Assistant MQTT 통합(MQTT Discovery) 공식 문서를 참고하세요.

이 문서의 entity 목록은 `bridge/ha_discovery.py` 를 기준으로 합니다.

---

## 동작 방식

브리지는 smartrelay 프로토콜을 다시 구현하지 않습니다.

| 무엇을 | 어디서 |
|---|---|
| 콘센트 상태 · 텔레메트리 | 릴레이 피드 규칙이 쓰는 `relay/state/devices.json` |
| 온라인 여부 | 릴레이 observer 의 세션 목록 (`{"list":true}`) |
| 콘센트 명령 | 릴레이 observer 주입 (기존 경로) |

- HA 스위치 상태는 **실제 텔레메트리**에서 옵니다. 명령을 보냈다고 바로 ON/OFF 로
  바꾸지 않습니다(discovery `optimistic: false`).
- 릴레이와 약결합입니다. 브로커 장애나 브리지 크래시가 릴레이·기기 로컬 제어·
  oneM2M 에 영향을 주지 않습니다.
- 권한 없는 `mttl` 사용자로 실행되며, sticky 매핑 파일을
  `/var/lib/mttl-ha-bridge/`(권한 `0700`)에 저장합니다.

## entity 구성

기기 하나당:

**기본 (10개, 모든 기기):**

- 스위치: `Outlet 1` ~ `Outlet 4`, `All` (5)
- 센서: `Power total`, `Power outlet 1` ~ `Power outlet 4` — 단위 W (5)

**v/t/p/e 텔레메트리를 보낸 기기 (`1.0.106` 계열, +10개 → 총 20개):**

- 센서: `Voltage` — V (1)
- 센서: `Internal temperature 1` ~ `4` — °C (4)
- 센서: `Energy total`, `Energy outlet 1` ~ `4` — kWh (5)

즉 기기당 **10개 또는 20개**입니다.

발행하지 않는 것: Wi-Fi 신호 세기 센서, "online" 바이너리 센서, 전류(current — 유선
데이터에 없음).

## 토픽

`<prefix>` 기본값은 `mttl`, discovery prefix 기본값은 `homeassistant` 입니다(환경
변수로 변경 가능).

| 종류 | 토픽 |
|---|---|
| 콘센트 명령 | `mttl/<slug>/outlet/<n>/set` — payload `ON` / `OFF` (`All` 은 `n=0`) |
| 콘센트 상태 | `mttl/<slug>/outlet/<n>/state` |
| 센서 상태 | `mttl/<slug>/power/total`, `.../power/<n>`, `.../voltage`, `.../temp/<n>`, `.../energy/total`, `.../energy/<n>` |
| 가용성 | `mttl/bridge/status` (브리지 프로세스), `mttl/<slug>/availability` (기기별) — `availability_mode: all` |

기기 블록: `identifiers = ["mttl_w01_<slug>"]`, `name = "MTTL-W01 <slug>"`,
`manufacturer = "LG U+ / TONLY"`, MAC 이 있으면 `connections`, 펌웨어 버전이 있으면
`sw_version`.

## 기기 식별 — slug

HA unique_id 와 토픽 경로는 안정적인 **slug** 로 고정됩니다.

- slug = 정상적인 MAC(콜론/하이픈 제거, 소문자). 예: `02:00:00:00:00:03` → `020000000003`
- MAC 을 얻지 못한 경우에만 client_id 의 SHA-256 앞 12자리(`d` + 12hex)
- **IP 는 절대 slug 에 쓰지 않습니다.**

브리지는 discovery 를 발행하기 전에 sticky MAC 을 먼저 해결합니다.

- **write-once**: 한 client_id 에 MAC 이 한 번 묶이면 그 매핑은 계속 유지됩니다.
  재시작·재배포·client_id 변경에도 같은 MAC slug 로 복원됩니다.
- 나중에 같은 client_id 에 **다른** MAC 이 오면 기존 매핑을 유지하고 경고만 남깁니다
  (교체/오프로비저닝된 기기가 다른 기기의 HA 신원을 가져가는 것을 막기 위함).
  재바인딩은 관리자가 매핑 파일을 직접 수정하는 수동 작업입니다.
- MAC 을 한 번도 제시한 적이 없는 기기는 discovery 를 보류합니다(해시 slug 를
  발행하지 않습니다).

### strict MAC 파싱

`bare 12-hex`, 일관된 `:` 구분, 일관된 `-` 구분 세 가지 형태만 MAC 으로 받습니다.
시리얼·IP·라벨을 12자리 16진수로 억지로 바꾸지 않습니다.

## 가용성 (availability grace)

- 45초 가용성 grace 로직은 **브리지 안에만** 있습니다(릴레이·대시보드는 관여하지
  않습니다). 환경 변수 `MTTL_HA_GRACE_SECONDS`, 기본값 `45`.
- 전력 센서에는 `expire_after` 가 없습니다. 부하가 일정하면 새 publish 가 없을 수
  있는데, `expire_after` 가 있으면 정상 기기를 잘못 "unavailable" 로 바꿀 수
  있기 때문입니다. 부재는 가용성 토픽으로만 표현합니다.

## reconciliation {#reconciliation}

브리지는 event-first 로 상태를 반영하고, 주기적으로 STATUS_GET 을 보내 상태를
맞출 수 있습니다(reconciliation).

- 환경 변수 `MTTL_HA_STATUS_INTERVAL`
- **코드 기본값은 `0`(OFF) 입니다.** 브리지는 이 값에서 주기적 STATUS_GET 을 보내지
  않습니다.
- `bridge/ha-bridge.env.example` 의 검증된/권장 배포 값은 **`30`(초)** 입니다.

> "기본값이 30초" 라고 이해하지 마세요. 코드 기본값은 OFF 이고, 이 프로젝트가 검증한
> 배포 환경에서 30초 reconciliation 을 사용한 것입니다.

브리지가 reconciliation 을 담당하는 경우, 대시보드의 자체 STATUS_GET
(`MTTL_STATUS_INTERVAL`, 대시보드 코드 기본값 `15`)은 `0` 으로 꺼서 두 곳에서
동시에 폴링하지 않도록 합니다.

명령 후 STATUS_GET:

- 개별 콘센트 명령: `MTTL_HA_POST_CMD_STATUS_DELAY`, 기본값 `0`(OFF)
- `All` 스위치: `POWER_SET` 후 약 1.5초(`MTTL_HA_ALL_CMD_STATUS_DELAY`) 뒤에 대상
  STATUS_GET 을 한 번 보내 콘센트 1~4 상태를 빠르게 당겨옵니다. 주기적
  reconciliation 과는 별개입니다.

## 환경 파일

`bridge/ha-bridge.env.example` 을 `/etc/mttl/ha-bridge.env` 로 복사해 채웁니다
(`chmod 600`). HA 브로커 값(`MTTL_HA_MQTT_HOST`, `MTTL_HA_MQTT_USERNAME`,
`MTTL_HA_MQTT_PASSWORD`)이 비어 있으면 브리지는 오류만 로그로 남기고 유휴
상태로 있습니다(observer 와 `devices.json` 은 계속 읽지만 아무것도 발행하지 않음).

주요 항목:

| 변수 | 예시 파일 값 | 설명 |
|---|---|---|
| `MTTL_HA_MQTT_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` | (비어 있음) / `1883` | HA Mosquitto 브로커 |
| `MTTL_HA_MQTT_TLS` / `_CA` | `0` | TLS + 자체 서명일 때만 CA 지정 |
| `MTTL_HA_MQTT_PREFIX` | `mttl` | 데이터 토픽 접두어 |
| `MTTL_HA_DISCOVERY_PREFIX` | `homeassistant` | discovery 토픽 접두어 |
| `MTTL_OBSERVER` | `127.0.0.1:9883` | 릴레이 observer |
| `MTTL_STATE_FILE` | `/srv/mttl-lab/relay/state/devices.json` | 릴레이 상태 파일 |
| `MTTL_HA_GRACE_SECONDS` | `45` | 가용성 grace |
| `MTTL_HA_STATUS_INTERVAL` | `30` | reconciliation 주기 (코드 기본값은 `0`) |
| `MTTL_HA_POST_CMD_STATUS_DELAY` | `0` | 개별 명령 후 STATUS_GET (OFF) |
| `MTTL_HA_CONFIRM_TIMEOUT` | `10` | 명령 확인 대기 |

---

[← README](../README.md)
