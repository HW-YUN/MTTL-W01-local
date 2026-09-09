# 펌웨어와 OTA

> ⚠️ 펌웨어 OTA 는 되돌리기 어려운 작업입니다. 잘못된 대상이나 잘못된 이미지는
> 기기를 손상시킬 수 있습니다. 이 문서를 끝까지 읽고, 자신의 기기와 이미지에 대해
> 확신이 있을 때만 진행하세요. 이 저장소는 펌웨어 바이너리를 포함하지 않습니다.

---

## 펌웨어 버전

| 버전 | 성격 |
|---|---|
| `1.0.66` | LG U+ 순정(stock) 펌웨어 |
| `1.0.105` | Voltra 계열 펌웨어 |
| `1.0.106` | 투오(tuoway) 님이 만든 커스텀 펌웨어. `af950833/mttl_w01` 이슈에서 다뤄진 QMS 관련 문제와 연관된 개선본. **본 프로젝트는 이 펌웨어를 그대로(재패치 없이) 사용해** smartrelay 기반 MEF / MQTT / oneM2M 구성에서 실기 검증했습니다 |
| `1.0.110` | 이후 투오(tuoway) 님이 공개한 별도의 Standalone(쉬운 설치) 구성용 커스텀 펌웨어. 중간 서버 없이 TCP 10086 backend 에 직접 연결하는 별도 구성이며 동작 구조가 다릅니다. `1.0.106` 과는 다른 버전입니다. **본 프로젝트는 `1.0.110` 을 사용하지 않습니다** |

`1.0.106` 바이너리가 어떤 과정으로 만들어졌는지는 확인되지 않았습니다(UNKNOWN).
본 프로젝트는 그 내부 구성을 다루지 않습니다.

## 검증 범위

본 프로젝트는 `1.0.106` 을 대상으로 다음을 실기 환경에서 확인했습니다.

- 로컬 릴레이를 통한 OTA 적용
- QMS 진단 업로드의 로컬 처리 (아래 참고)
- 클라우드 없이의 로컬 런타임
- Home Assistant 의 전력 · 에너지 · 전압 · 온도

`1.0.66` 순정 이미지는 롤백 기준으로 참고합니다.

## 펌웨어 준비

이 저장소는 `1.0.66` / `1.0.105` / `1.0.106` / `1.0.110` 바이너리를 재배포하지
않습니다. 사용할 이미지는 원 출처(예: `1.0.106` 은
[af950833/mttl_w01](https://github.com/af950833/mttl_w01) 및 관련 이슈)에서 직접
확인해 자신의 환경에 준비하세요.

준비한 `.fwr` 파일의 SHA-256 을 계산해 두세요. OTA 규칙이 매 요청마다 이 값을
대조합니다.

## OTA 규칙의 안전 모델

`relay/rules/15-firmware-ota.py` 는 다음 원칙으로 동작합니다.

- **단일 대상 (single-target).** 지정한 기기 한 대에만 이미지를 제공합니다. 그 밖의
  모든 요청은 명시적으로 "업데이트 없음" 으로 차단(BLOCK)됩니다(HTTP 200, 빈 본문).
- **fail-closed.** 대상이 설정되지 않으면 모든 요청을 차단합니다.
- **deny-first.** 차단 목록의 IP / origin 은 다른 판정보다 먼저 거부됩니다.
- 제공(OFFER)은 다음을 **모두** 만족할 때만 이루어집니다: 소스 IP == 대상 IP,
  `X-M2M-Origin` == 대상 origin, 유효하고 만료되지 않은 ARM 파일(대상 정보 일치),
  `sha256(펌웨어) == ARM 의 sha256` (매 요청 재확인).
- 상태 기계: `OFF → ARMED → OFFERED → TRANSFERRING → SPENT`. `SPENT` 는 종료
  상태이며 릴레이 재시작 후에도 유지됩니다(ARM 파일이 이름이 바뀌어 치워집니다).
- ARM 없음 / 만료 / 형식 오류 / sha 불일치 / 예상 밖 경로 / 예외 → 모두 차단.

## 설정 (환경 변수)

| 변수 | 필수 | 설명 |
|---|---|---|
| `MTTL_OTA_TARGET_IP` | 예 | 업데이트할 기기의 소스 IP |
| `MTTL_OTA_TARGET_ORIGIN` | 예 | 그 기기의 `X-M2M-Origin` / entityId |
| `MTTL_OTA_DENY_IPS` | 아니오 | 항상 차단할 IP (쉼표 구분) |
| `MTTL_OTA_DENY_ORIGINS` | 아니오 | 항상 차단할 origin (쉼표 구분) |
| `MTTL_OTA_ARM_FILE` | 아니오 | ARM 파일 경로 (기본 `/etc/mttl/firmware-ota.arm`) |
| `MTTL_OTA_STATE_FILE` | 아니오 | 진행 상태 파일 경로 (기본 `/srv/mttl-lab/relay/state/firmware-ota.state`) |

대상(`MTTL_OTA_TARGET_IP` + `MTTL_OTA_TARGET_ORIGIN`)을 설정하지 않으면 어떤 요청도
제공되지 않습니다.

## ARM 파일

운영자가 직접 작성하는 `key=value` 텍스트 파일입니다. 규칙은 이 파일을 읽기만
합니다.

| 키 | 내용 |
|---|---|
| `target_ip` / `target_origin` | 환경 변수의 대상과 일치해야 함 (ARM 파일로 대상을 바꿀 수 없음) |
| `fwr` | `.fwr` 이미지의 **절대 경로** |
| `sha256` | 그 이미지의 SHA-256 |
| `offer_version` / `fwnnam` | 기기에 제시할 버전 문자열 / 파일 이름 |
| `expires_epoch` | ARM 만료 시각 (epoch) |

기기의 요청 흐름: `/mef/updateVersionCheck/firmware/...` → 제공 → `.fwr` GET
(`/mef/firmware<버전>/<이름>` 또는 `/mef/firmware/MTAP/20/D/<버전>/<이름>`). 전송은
4096바이트 단위로, 헤더 없는 재요청은 120초 이내로만 연관지어 처리합니다.

전송이 완전히 끝나면(`SPENT`) ARM 파일이 치워지고, 그 이후로는 다시 차단
상태입니다.

## QMS {#qms}

`relay/rules/16-qms-sink.py` 는 기기의 진단 로그("QMS") HTTPS 업로드를 **로컬에서
관찰만** 하는 규칙입니다.

- 대상 호스트: `log.toi.ommeq.com` (`1.0.105` 의 QMS 엔드포인트, `POST /read_iot_wifi`),
  `hdslog.lguplus.co.kr` (`1.0.66` 실행 기록에서 관찰됨 — 같은 서비스인지는
  확인되지 않아 둘 다 sink 합니다).
- 기록하는 것: 소스 IP, 시각, 호스트, 메서드, 경로, `Content-Length`, 일부
  비민감 본문 표식.
- **진단 페이로드(DiagLog) 전체는 저장하지 않습니다.**
- 어떤 실제 LG / Ommeq / Voltra 엔드포인트로도 전달하지 않습니다.
- 기기에는 무해한 `200 OK` 를 돌려줍니다. 잘못된 시점의 관찰이 펌웨어의 QMS
  재시도 타이머를 작동시키지 않도록 하기 위함입니다.
- 상태·로그 파일: `/srv/mttl-lab/relay/state/qms-sink.state`, `.../qms-sink.log`
  (JSON Lines). 테스트용으로 `MTTL_QMS_SINK_STATE_FILE` / `MTTL_QMS_SINK_LOG_FILE`
  로 경로를 바꿀 수 있습니다.

QMS 호스트를 릴레이로 향하게 하는 DNS 설정(선택)은 [network.md](network.md#qms-sink-dns-선택)
을 참고하세요.

## 다른 버전으로의 전환

다른 버전(예: 순정 `1.0.66` 으로의 복귀)으로 전환하려면 동일한 OTA 흐름을 사용해
해당 이미지를 준비하고 ARM 파일에 그 경로·해시·버전을 지정하면 됩니다. 본
프로젝트가 검증한 것은 `1.0.106` 적용까지이며, 다운그레이드는 검증하지 않았습니다.
위험을 감안해 사용자가 판단하세요.

---

[← README](../README.md)
