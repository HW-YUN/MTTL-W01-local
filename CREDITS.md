# CREDITS

이 프로젝트는 여러 커뮤니티 프로젝트와 분석 결과 위에서 만들어졌습니다. 아래에
사람·프로젝트·허락 출처를 정리합니다. 기술적 참고자료 링크는
[docs/references.md](docs/references.md) 를 참고하세요.

---

## smartrelay — 3735943886

- 저장소: <https://github.com/3735943886/smartrelay>

본 프로젝트의 로컬 릴레이 스택은 **smartrelay** 를 기술 기반으로 사용합니다.
공개 저장소에는 다음이 포함되어 있습니다.

- smartrelay 원본 파일 일부 (`vendor/smartrelay/` 아래)
- 로컬 패치가 적용된 `vendor/smartrelay/relay.py`, `vendor/smartrelay/rules_engine.py`
  - 패치 1: 세션 재연결 시 오래된 연결이 닫히는 경쟁 조건 수정
  - 패치 2: OTA 전송을 위한 스트리밍 응답(`HttpResponse.stream`) 추가
- `relay/rules/99-default.py` — smartrelay 의 `rules/99-default.py` 를 저장소 트리에
  맞게 옮겨 담은 사본입니다. 맨 앞 4줄에 출처를 표시하는 주석만 추가했고, 그
  이후 본문은 원본과 바이트 단위로 동일합니다(`tests/synthetic_tests.sh` 가 이를
  검증합니다).

**공개·재배포 허락:** 위 원본 파일과 로컬 수정본을 본 공개 저장소에 포함해
공개·재배포하는 것에 대해 원작자에게 **별도로 허락을 받았습니다.**
원작자 `3735943886` 님이 smartrelay 저장소의 GitHub Issue #3 에서 수정본을 포함한
공개·재배포가 괜찮다고 직접 답변했고, 해당 이슈를 completed 로 종료했습니다.

이 허락의 정확한 범위는 **"이 프로젝트에서 문의하고 허락받은 파일과 수정 범위의
공개·재배포"** 입니다. 그 이상으로 확대 해석하지 않습니다.

> ⚠️ smartrelay 저장소에는 별도의 오픈소스 라이선스(MIT / Apache / GPL 등)가
> 부여되어 있지 않습니다. 따라서 이 문서나 저장소의 어디에서도 smartrelay 를
> "MIT 라이선스" 등으로 표현하지 않습니다. 위 재배포는 라이선스가 아니라
> **원작자의 개별 허락**에 근거합니다.

본 저장소가 직접 작성한 코드·설정 예시·배포 스크립트·문서는 저장소 루트
[LICENSE](LICENSE) 의 MIT 조건을 따릅니다. 위 `vendor/smartrelay/**` 및
`relay/rules/99-default.py` 는 MIT 적용 대상이 아니며, 이들의 공개·재배포는 위에
설명한 Issue #3 의 개별 허락에 근거합니다(정확한 범위는 이 절의 provenance·허락
설명을 따릅니다).

---

## mttl_w01 — af950833

- 저장소: <https://github.com/af950833/mttl_w01>

Docker 기반의 로컬 서버 구성을 제공하는 프로젝트입니다. Docker 중심으로 MTTL-W01 을
로컬화하려는 경우 이 프로젝트를 참고하시기 바랍니다(본 프로젝트는 Docker 구성을
검증하지 않습니다).

`1.0.106` 커스텀 펌웨어는 이 저장소와 그 이슈를 통해 공개되어 논의되었습니다.

---

## 투오(tuoway) — 커스텀 펌웨어

네이버 카페 활동명 **투오**, GitHub handle `tuoway` (동일 인물). `af950833/mttl_w01`
GitHub Issue #2("전압,온도,전력 리포팅과 QMS 보고 꺼놓은 펌웨어 버전 공유")에서
MTTL-W01 `1.0.106` 펌웨어 패치를 직접 공개했고, 이후 `1.0.110` 펌웨어도 제공했습니다.

- `1.0.106` — `af950833/mttl_w01` 이슈에서 다뤄진 QMS 관련 문제와 연관된 개선
  커스텀 펌웨어. 본 프로젝트는 이 펌웨어를 그대로(재패치 없이) 사용해 실기 환경에서
  검증했습니다.
- `1.0.110` — 이후 공개된 Standalone(쉬운 설치) 구성용 커스텀 펌웨어. `1.0.106`
  계보의 후속 발전형이며 동작 구조가 다릅니다. 본 프로젝트는 `1.0.110` 을
  사용하지 않습니다.

자세한 펌웨어 계보 설명은 [docs/firmware-ota.md](docs/firmware-ota.md) 를 참고하세요.

---

## 순정 펌웨어 TCP 10086 직접 제어 — 탱즈(ttaengz)

순정 펌웨어의 TCP 10086 경로로 기기를 직접 제어하는 접근 방식(polling 기반 상태
갱신)을 정리·공유한 분입니다. 본 프로젝트의 로컬 릴레이 방식과는 다른 접근이며,
이후 SmartThings 중심 환경을 위해 Matter Bridge 서버 방식으로 확장했습니다:
<https://github.com/ttaengz/mttl-w01-matterbridge>

---

## 커뮤니티

MTTL-W01 로컬화 관련 정보를 공유해 온 네이버 카페 및 Home Assistant 관련
커뮤니티의 게시물 작성자분들께 감사드립니다. 개별 게시물의 공개 링크가 확인된
경우 [docs/references.md](docs/references.md) 에 정리합니다.

---

## 감사의 말

- **`3735943886`** — GitHub 사용자 `3735943886`, `smartrelay` 원작자. 이 저장소에
  포함된 원본·수정 smartrelay 코드의 공개·재배포를 허락해 주셨습니다. 정확한 허락
  범위와 출처는 위 smartrelay 절을 따릅니다.
- **`af950833`** — GitHub 사용자 `af950833`. `mttl_w01` 저장소를 통해 MTTL-W01
  로컬화 접근법과 펌웨어 공개·논의의 기반을 마련해 주셨습니다.
- **투오(tuoway)** — 네이버 카페 활동명 `투오`, GitHub handle `tuoway` (동일 인물).
  `1.0.106` 및 이후 `1.0.110` 커스텀 펌웨어를 제공해 주셨습니다.
- **탱즈(ttaengz)** — 순정 펌웨어 TCP 10086 직접 제어 접근 등, 문서에서 확인된
  기술적 참고 기여에 감사드립니다.

### AI 도구

- **ChatGPT (OpenAI)** — 프로젝트 계획 수립, 기술 분석과 교차검토, 문서 구조화,
  공개 준비를 보조했습니다.
- **Claude Code (Anthropic)** — 사용자와 ChatGPT 가 승인한 범위에서 코드·파일 작업과
  검증 명령 실행, 공개 준비 작업을 수행했습니다.

AI 도구는 프로젝트의 계획·분석·검증·작업 보조에 사용되었으며, 최종 판단과 공개
결정은 사용자에게 있습니다. GitHub 저장소의 자동 Contributors 집계는 실제 커밋을
작성한 당사자에게만 맡깁니다.

---

[← README](README.md)
