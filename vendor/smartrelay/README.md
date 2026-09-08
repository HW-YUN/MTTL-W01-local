## 이 리포가 하는 일

1. **프로비저닝** — SoftAP `LGAPMODE` 프로토콜로 기기에 홈 WiFi 자격증명 주입 (`provision.py`)
2. **투명 릴레이(Proxy)** — 기기가 원하는 목적지로 향하게, 우리 CA/리프로 기기측을 종단하고
   업스트림은 직접 지정한 실제 목적지로 재암호화 중계 (`relay.py`)
3. **양방향 패킷 캡처** — 위 릴레이를 통과하는 평문 트래픽을 양방향으로 파일 로깅
   (`relay.py`, `--capture-dir` 지정시). oneM2M/MQTT 브로커 프로토콜 문서화는 계속 진행 중.
4. **로컬 전용(Decloud) + observer 주입** — `--dns`를 안 주면(또는 조회 실패시)
   업스트림 없이 `relay.py`가 `rules/*.py`(응답 규칙 계약은 `rules_engine.py`)로
   HTTP(`:80`/`:443`)와 MQTT(`:18831`)를 직접 서빙한다 — 실서버 없이
   기기가 정상 동작. `--observer HOST:PORT`로 평문 MQTT 로컬주입 포트를 열면 외부 도구가
   보낸 명령(`{"outlet":1,"on":true}`)이 실제 기기 세션에 oneM2M `device_control`로
   주입된다(Proxy/Decloud 둘 다 지원). 자세한 계약은 `rules_engine.py` 상단 docstring,
   기기별 응답 값은 `rules/99-default.py` 참고.

---

## 1. 프로비저닝 — `provision.py`

`LGAPMODE` 바이너리 프로토콜 범용 CLI. 표준 라이브러리만 사용, 추가 의존성 없음,
Linux/macOS/Windows 어디서나 동작 — OS 네트워크 관리자(nmcli 등)에 의존하지 않음.

**이 도구는 SoftAP 연결을 대신 해주지 않는다.** 기기 SoftAP에는 사용자가 직접
OS의 WiFi 설정에서 수동으로 접속해야 한다.

```bash
# 1) (SoftAP에 수동 연결 후) 기기 정보 조회 — 선택
python3 provision.py info

# 2) 홈 WiFi 자격증명 주입 — 기본은 dry-run, 실제 전송은 --yes
python3 provision.py inject-wifi --home-ssid "MyHomeWifi" --home-password "hunter2"
python3 provision.py inject-wifi --home-ssid "MyHomeWifi" --home-password "hunter2" --yes

# 3) 재시작 트리거 — 홈 WiFi로 전환
python3 provision.py reset --yes
```

> ⚠️ **`reset`을 실제로 전송하기 전에(`--yes`), `relay.py serve`와 DNS 리다이렉션을
> 먼저 띄워둘 것** — 리셋 직후 기기가 곧바로 클라우드 접속을 시도하고, 실패하면
> 몇 번 재시도하다 완전히 포기해버린다(2번 섹션 참고). 순서가 바뀌면 SoftAP
> 재프로비저닝부터 다시 해야 한다.

기기가 아직 SoftAP에 안 붙어 있으면 각 명령은 자동으로 연결을 시도하지 않고
수동 연결 안내만 출력한 뒤 종료한다. `--wait N`을 주면 N초 동안 재시도하며
그 사이 수동으로 붙기를 기다린다(연결 자체는 여전히 하지 않음).

```bash
python3 provision.py info --wait 30
```

## 2. 투명 릴레이 & 양방향 패킷 캡처 — `relay.py`

기기가 프로비저닝 후 신뢰하는 CA를 우리 것으로 교체하면, 기기는 우리가 세운
TLS 종단을 실제 서버로 착각한다. `relay.py`는 도메인을 미리 몰라도 된다 —
**완전 동적**이다:

1. 루트 CA를 한 번만 만들어서 계속 재사용(`--cert-dir`에 저장).
2. TLS 연결이 들어오면 `ClientHello`의 **SNI**(암호화되기 전 평문 필드)로 어떤
   도메인인지 알아낸다. 그 도메인용 리프 인증서가 없으면 그 자리에서 그 CA로
   서명해 만들고(캐싱, 다음부턴 재사용) 그걸로 device 쪽 TLS를 종단한다.
3. `--dns`로 지정한 DNS 서버에 그 도메인을 직접 질의해서 실제 IP를 얻는다.
   성공하면 그 IP로 재암호화 연결해서 투명 릴레이(**Proxy**). `--dns`를 안 줬거나
   조회/연결에 실패하면 업스트림 없이 **Decloud**로 폴백한다 — `rules/*.py`가 직접
   HTTP/MQTT를 서빙(아래 "로컬 전용(Decloud)" 절 참고). rules가 아무것도 응답 안 하면
   그냥 관찰만(가장 안전).

즉 도메인을 몇 개 미리 나열할 필요가 없다 — 뭐가 오든 SNI로 배우고, 실제
목적지는 그때그때 DNS로 찾는다.

**단, SNI를 아예 안 보내는 기기가 실제로 있다.**
임베디드 TLS 스택 중엔 `ClientHello`에 SNI 확장 자체를 안 넣는 경우가 흔하다.
이러면 SNI로 도메인을 배울 수가 없어서 기본적으로 인증서 발급을 거부하고
연결이 끊긴다. 이럴 때는 `--default-domain <도메인>`으로 SNI가 없을 때 쓸
도메인을 직접 지정해야 한다 — 인증서 발급도 `--dns` 조회도 그 도메인으로
처리된다.

양방향 평문 트래픽은 연결별로 파일에 그대로
로깅된다(`captures/<연결ID>_device_to_upstream.bin`, `..._upstream_to_device.bin`)
— 이게 바로 목표 3(양방향 패킷 캡처)이기도 하다: 별도 도구 없이 릴레이가 곧
캡처다. 화면에는 연결/인증서 발급/DNS 조회/릴레이 시작·종료 같은 요약 정보만
실시간으로 찍히고, 바이트 단위 상세는 화면이 아니라 파일에 쌓인다.

여기서 캡처되는 건 **TLS로 감싸인 HTTPS든 MQTTS든 그 위의 애플리케이션 데이터
전체**다 — `relay.py`는 HTTP/MQTT를 구분하지 않고 TLS 안의 바이트를 그대로
주고받기만 하므로, MQTT `PINGREQ`/`PINGRESP` 같은 heartbeat도 포함해서 연결이
살아있는 동안의 모든 페이로드가 잡힌다. 단, 이건 TCP `SYN`/`ACK` 같은 순수 제어
패킷 얘기가 아니다 — 그런 건 페이로드가 없어서 애초에 애플리케이션 계층까지
올라오지 않는다(커널이 처리). TCP 레벨 캡처가 별도로 필요하면 같은 머신에서
`tcpdump`를 병행할 것 — `relay.py`는 그 역할을 하지 않는다.

### ⚠️ 필수 전제조건 — DNS 리다이렉션 (기기 쪽)

대상 기기가 실제로 조회하는 도메인의 **DNS 응답이 `relay.py`를 실행하는 머신의 LAN IP로 나가야 한다.**
DNS를 안 바꾸면 기기는 실서버로 곧장 나가버리고 `relay.py`는 어떤 연결도 받지
못한다. `relay.py` 자체는 기기용 DNS 서버가 아니므로 별도로 처리해야 한다 —
예를 들어 라우터/AP에 dnsmasq를 쓴다면:

```
# dnsmasq.conf
address=/<대상 도메인>/<relay.py를 실행하는 머신의 LAN IP>
```

(라우터 DNS를 통째로 바꿀 수 없다면 기기 트래픽이 지나는 지점에서 동일한 효과를
내는 다른 방법 — 로컬 DNS 리졸버 지정, ARP 스푸핑 등 — 을 직접 구성해야 한다.)

**`--dns`는 이거랑 정반대 역할이라 헷갈리면 안 된다.** `--dns <IP>`는
`relay.py`가 진짜 업스트림 IP를 알아내려고 **직접 질의하는** 서버다(보통
`8.8.8.8` 같은 공용 DNS). 위 dnsmasq(기기용, 조작된 답을 주는 쪽)와 절대
같은 서버면 안 된다 — 같으면 우리 자신이 조작한 답을 되돌려받아 자기 자신에게
연결하려 하게 된다.

### ⚠️ 필수 전제조건 — 포트는 공유 불가, 관리자 권한 필요

`--http-port`(기본 80)와 `-p/--port`로 지정하는 모든 로컬 포트는 이 머신에서
`relay.py`가 **단독으로** bind/listen 해야 한다. 다른 프로세스(웹서버, 다른
`relay.py` 인스턴스 등)가 이미 그 포트를 쓰고 있으면 실행 즉시 에러로 종료된다
— 포트 하나에 리스너 하나뿐, 공유되지 않는다. 80/443처럼 1024 미만 포트는
대부분의 OS에서 관리자 권한이 필요하다(Linux면 `sudo`로 실행하거나
`setcap cap_net_bind_service=+ep` 부여).

### ⚠️ 필수 순서 — `relay.py`가 먼저, `provision.py reset`은 그 다음

`provision.py reset`으로 기기를 리셋하면 기기는 곧바로 DNS 조회 → CA 다운로드
→ TLS 핸드셰이크를 시도하고, 실패하면 몇 차례 지수 백오프 후 완전히 재시도를
포기한다. **DNS 리다이렉션과 `relay.py serve`가 먼저 떠 있는 상태에서**
`provision.py reset`을 트리거해야 한다 — 순서가 바뀌면 기기가 재시도를 다
소진해버리고, 다시 잡으려면 SoftAP 재프로비저닝부터 반복해야 한다.

### 사용법

```bash
# 포트만 열고 관찰만 (가장 안전, --dns 없음). SNI를 보내는 기기라면 이걸로 충분.
python3 relay.py serve -p 443

# SNI를 안 보내는 기기 — 기본 도메인을 직접 지정
python3 relay.py serve -p 443 --default-domain <대상 도메인>

# 여러 포트 동시에(예: HTTPS용 443 + MQTTS 브로커용 8883), 실제 DNS로 업스트림 찾아서 릴레이
python3 relay.py serve -p 443 -p 8883 --default-domain <대상 도메인> --dns 8.8.8.8

# 로컬에선 비표준 포트로 받고 업스트림은 표준 포트로 (LOCAL:REMOTE)
python3 relay.py serve -p 18883:8883 --default-domain <대상 도메인> --dns 8.8.8.8

# 포트마다 다른 기본 도메인을 직접 붙이기(LOCAL:REMOTE:DOMAIN) — 이 포트에 한해
# --default-domain보다 우선. 여러 서비스가 포트별로 다른 도메인을 쓸 때 유용.
python3 relay.py serve -p 443:443:<도메인A> -p 18831:18831:<도메인B> --dns 8.8.8.8

python3 relay.py gen-ca --force      # 루트 CA만 미리 만들어두거나 강제 재생성(선택 — 자동 생성됨)
```

`-p/--port`는 반복하거나 콤마로 여러 개 줄 수 있다. 각 항목은 `PORT`(로컬=업스트림
같은 포트) 또는 `LOCAL:REMOTE`(로컬 포트와 실제 접속할 업스트림 포트가 다를 때).
SNI를 보내는 기기라면, 같은 머신으로 여러 서브도메인(예: `a-1.a.com`,
`a-2.a.com`)이 전부 리다이렉션돼 있어도 별도 설정 없이 자동으로 처리된다 —
SNI로 도메인을 구분해서 도메인별로 인증서를 따로 발급하고, `--dns`로 그
도메인마다 실제 IP를 각각 찾아서 릴레이한다. (SNI를 안 보내는 기기는
`--default-domain`이 도메인 하나만 받으므로 이 자동 구분이 적용되지 않는다 —
그런 기기가 여러 도메인을 쓴다면 포트/머신을 분리해서 인스턴스를 따로 띄울 것.)

### ⚠️ 안전 — `--dns`를 지정했다면

`--dns`를 지정하면 실제 서버의 응답이 그대로 기기에 전달된다. 그 응답에
펌웨어/OTA 지시가 섞여 있으면 그대로 전달된다는 뜻이다 — 화면 로그를 보면서
수상한 응답이 보이면 즉시 프로세스를 죽일 것(자동 차단은 없음). Decloud는
우리가 rules로 직접 응답을 만드니 이 위험 자체가 없다.

### 설정 파일(`smartrelay.toml`) — 앱/도커 배포용

CLI 인자 대신 실행 시점의 working dir에 `smartrelay.toml`을 놓으면 `relay.py serve`가
자동으로 읽는다(없어도 에러 아님 — 그냥 CLI/기본값으로 진행). 값 우선순위는
**CLI 인자 > 설정 파일 > 코드 기본값**이라, 필요한 항목만 CLI로 덮어쓸 수 있다.
포맷은 TOML — 이 리포가 표준 라이브러리만 쓴다는 원칙을 지키면서(Python 3.11+의
`tomllib`, 추가 의존성 0) 포트 목록 같은 중첩 구조를 INI보다 깔끔하게 표현할 수 있어서
골랐다(YAML은 PyYAML 외부 의존성이 필요해서 제외).

```bash
cp smartrelay.toml.example smartrelay.toml   # 값 채운 뒤
python3 relay.py serve                        # 인자 없이 실행 — smartrelay.toml을 읽음
# 다른 경로를 쓰고 싶으면:
python3 relay.py serve --config /etc/smartrelay/smartrelay.toml
```

도커 배포시엔 이미지에 `smartrelay.toml`을 굽거나 볼륨/바인드마운트로 working dir에
올려두면 된다(설정값 자체는 `.gitignore`에 포함되어 있어 커밋되지 않음 —
`smartrelay.toml.example`만 리포에 남는다).

## 3. 로컬 전용(Decloud) + observer 로컬 주입

`--dns`를 안 주면(또는 조회/연결 실패시) `relay.py`는 업스트림 없이 그 자리에서
HTTP(`:80`/`:443`)와 MQTT(`:18831`)를 직접 서빙한다 — 실 클라우드 서버 없이도
기기가 정상 동작(CSE 부트스트랩 통과, 상태 리포트 수신)한다:

```bash
sudo python3 relay.py serve -p 443:443:<대상 도메인> -p 18831:18831:<대상 도메인> \
  --http-port 80 --rules-dir rules --observer 127.0.0.1:9883
```

`rules/*.py`(파일명 알파벳순, 핫리로드 — 계약은 `rules_engine.py` docstring)가
실제 응답을 만든다. **엔진(`relay.py`)엔 기기별 하드코딩이 0줄** — 기기 특유의
값(CSE 부트스트랩 golden 응답, `:443 POST /mef` 인증 응답, `device_control` 포맷)은
전부 `rules/99-default.py`에 있다. 다른 기기/버전을 다루고 싶으면 이 파일보다
먼저 정렬되는 이름(예: `10-other-device.py`)으로 새 규칙 파일을 추가할 것 —
파일명 순서대로 먼저 매칭되는 게 이긴다(nginx conf.d 관례).

**observer**(`--observer HOST:PORT`)는 평문 MQTT 리스너를 하나 더 연다(로컬신뢰망
전용, 방화벽으로 반드시 보호할 것). 여기로 JSON 커맨드를 PUBLISH하면
`rules/*.py`의 `on_local_inject()`가 실제 oneM2M `device_control` 메시지로 번역해서
현재 붙어있는 기기 세션에 그대로 주입한다(Proxy든 Decloud든 무관하게 동작 —
Proxy에서도 기기의 `CONNECT`를 살짝 엿봐서 주입 대상을 확보해둔다):

```bash
# 전원 제어 (action 생략시 기본값). outlet 0 = 전체
mosquitto_pub -h 127.0.0.1 -p 9883 -t mtap/cmd -m '{"outlet":1,"on":true}'

# 즉시 상태 보고 요청(STATUS_GET)
mosquitto_pub -h 127.0.0.1 -p 9883 -t mtap/cmd -m '{"action":"status"}'

# 대기전력 컷오프 임계값 설정(outlet 1~4만, threshold_centiwatt는 0.01W 단위)
mosquitto_pub -h 127.0.0.1 -p 9883 -t mtap/cmd \
  -m '{"action":"configuration","outlet":1,"threshold_centiwatt":500,"enabled":true}'
```

기기->서버 텔레메트리(`STATUS`/`METER`/`CONFIGURATION`/`ALARM` 이벤트)는 이제
`rules/99-default.py`가 파싱해서 client_id별로 메모리에 캐시하고(`전원 on/off`,
`전력(W)`, `누적 에너지`, `대기전력 설정`, `알람`) 콘솔에 요약 로그를 찍는다 — 예전엔
그냥 버렸었다. 단, 주기적으로 먼저 `STATUS_GET`을 보내는 자동 폴링은 없다(기기가
스스로 리포트할 때만 갱신) — 필요하면 위 `{"action":"status"}`를 직접 주기적으로
호출할 것.

## 안전 수칙

- 본인 소유 기기에서만 사용할 것.
- 프로비저닝 후 기기가 서드파티 클라우드에 자동 등록되면 펌웨어가 비가역적으로
  교체될 수 있다. 재현 시 CCS/OTA 트래픽 차단 등 안전장치를 유지할 것.
