# Blind Test Bot — 고도화 계획

현재 상태: MVP 완성. 파이프라인 동작 확인됨. 아래는 프로덕션 품질을 위해 개선할 사안들.

---

## 1. 안정성 (Stability)

### 1-1. 인메모리 상태 누수
`test_orchestrator.py` 의 모듈 레벨 딕셔너리는 서버가 살아있는 동안 무한 증가.
- **단기**: 완료된 run에 TTL 설정 + 주기적 GC (e.g., 24시간 후 자동 삭제)
- **장기**: SQLite 또는 Redis로 영속성 확보

### 1-2. AI Validator 배치 폴링 타임아웃 없음
`ai_validator.py` 의 배치 폴링이 30초 간격으로 무한 대기 가능.
- 최대 대기 시간 설정 (예: 10분) 후 heuristic fallback 강제
- `tc_batch_delay_seconds` 처럼 `ai_validate_timeout_seconds` 설정값 추가

### 1-3. Fallback 투명성
heuristic validator로 fallback될 때 사용자가 모름.
- 결과에 `validation_mode: "ai" | "heuristic"` 필드 추가
- 로그에 fallback 이유 명시

### 1-4. tc_planner 단일 실패점
대용량 스펙에서 tc_planner 호출 하나가 실패하면 plan 없이 진행.
- 스펙이 N개 엔드포인트 초과 시 청크로 나눠서 계획 수립
- 재시도 로직 추가 (현재 없음)

---

## 2. TC 품질 (Test Case Quality)

### 2-1. local_tc_generator 커버리지 확장
현재 없는 TC 타입:
- **경계값 테스트**: 빈 문자열, 최대 길이 초과, 음수, 0
- **IDOR**: 다른 유저 ID로 접근 시도
- **error_disclosure**: 잘못된 타입/형식으로 500 유도
- **배열/중첩 객체**: request body에 배열이 있을 때 단일값 전송

### 2-2. Claude TC 생성 fallback
Claude API 오류 시 해당 배치를 조용히 스킵함 (`tc_generator.py` L230-235).
- 실패한 엔드포인트 → 자동으로 local_tc_generator로 재생성
- 스킵된 엔드포인트 목록을 로그 및 응답에 노출

### 2-3. TC 중복 제거
Claude가 같은 엔드포인트를 여러 배치에서 처리할 때 중복 TC 생성 가능.
- TC 생성 후 `(method, path, description)` 기준 중복 제거

### 2-4. Postman 컨텍스트 활용도 향상
현재 첫 3개 예시만 Claude에게 전달.
- 인증 헤더는 전부 전달 (보통 1-2개)
- 바디 예시는 용량 기반 동적 결정 (토큰 예산 관리)

---

## 3. 스펙 파싱 (Spec Parsing)

### 3-1. 대용량 PDF 처리
현재 18,000자 하드코딩 상한. 대형 API 문서 파싱 불완전.
- 스펙 크기에 따라 동적으로 청크 분할 후 병렬 파싱
- 엔드포인트 추출 후 병합 및 중복 제거

### 3-2. 외부 `$ref` 지원
OpenAPI 스펙이 여러 파일로 분리된 경우 (`$ref: ./schemas/User.yaml`) 현재 무시됨.
- 파일 업로드 시 ZIP 지원 (메인 스펙 + 참조 파일들)
- URL 기반 `$ref` resolve (공개 스펙 한정)

### 3-3. 추가 포맷 지원 (장기 / 모델 재설계 필요)
> **주의**: `EndpointSpec`이 `method + path + parameters` REST 구조로 설계되어 있어, 아래 포맷은 파서 추가만으로 해결 안 됨 — 내부 모델 추상화 재설계가 전제.

- **GraphQL**: 단일 `POST /graphql` endpoint에 query string으로 동작 → `EndpointSpec`으로 표현 불가. `OperationSpec` 같은 별도 모델 필요
- **gRPC / Protobuf**: `.proto` 파일 → 현재 HTTP 기반 executor로 직접 실행 불가. gRPC-HTTP 트랜스코딩 레이어 필요
- **RAML**: 상대적으로 REST 친화적 → 중기 과제로 가능

---

## 4. 실행 엔진 (Executor)

### 4-1. Bearer token 만료 재발급
`auth_headers` 는 run 시작 시 정적으로 주입되므로, 실행 중 token이 만료되면 이후 TC 전체가 401로 실패.
> 참고: login → token 추출 → 이후 단계 주입은 `TestScenario + extract` 로 이미 지원됨. 여기서의 갭은 **단독 TC 실행 중 token 갱신** 문제.

- `ExecuteConfig` 에 `auth_refresh` 설정 추가: `{url, body, token_path}`
- 실행 중 401 응답 감지 시 자동으로 refresh 엔드포인트 호출 → 새 token으로 헤더 교체 후 재시도

### 4-2. ~~요청 전처리 훅~~ (제거)
~~TC에 `pre_request_script` 필드 추가 (JavaScript or Python snippet)~~

서버 사이드 임의 코드 실행은 RCE(원격 코드 실행) 위험. 샌드박싱 없이는 도입 불가.
별도 에이전트 아키텍처가 필요한 장기 과제로 이관 — 현재 범위에서 제외.

### 4-3. 응답 기록 개선
현재 응답 바디를 통째로 저장. 대용량 응답 시 메모리 문제.
- 응답 바디 최대 크기 설정 (기본 10KB, 초과 시 truncate + 표시)
- 이진 응답 (PDF, 이미지) 처리 (현재 text fallback)

---

## 5. 관찰가능성 (Observability)

### 5-1. 비용 추적
Claude API 호출마다 토큰 수와 예상 비용을 run 결과에 기록.
- `TestRunStatusResponse` 에 `estimated_cost_usd` 필드 추가
- 모델별 토큰 단가를 `config.py` 에서 관리

### 5-2. 실행 메트릭
- 엔드포인트별 평균 응답 시간
- PASS/FAIL 비율 히트맵
- 가장 느린 엔드포인트 TOP N

### 5-3. 구조화된 로그
현재 로그는 사람이 읽는 문자열. 파싱하기 어려움.
- JSON Lines 포맷 옵션 추가
- 이벤트 타입 태깅 (`parse_complete`, `tc_generated`, `execution_result`, ...)

---

## 6. API / UX

### 6-1. 결과 페이지네이션
`GET /results` 가 결과 전체를 한 번에 반환. TC가 수백 개면 응답이 큼.
- `?page=1&page_size=50` 쿼리 파라미터 추가
- 또는 커서 기반 페이지네이션

### 6-2. 생성 전 비용 근사 추정
> **범위 제한**: "추정하려면 Claude를 호출해야 하고, 호출하면 이미 비용이 발생"하는 닭-달걀 문제. 정확한 추정 대신 **엔드포인트 수 기반 근사치** 제공.

- `GET /test-runs/{run_id}/estimate?strategy=standard&generator=claude`
  → `{endpoint_count: N, estimated_tc_count: N*5, estimated_tokens: ~N*800, estimated_cost_usd: X}`
- 근사 공식: `엔드포인트 수 × 전략별 평균 TC 수 × 모델 단가(config에서 관리)`
- 실제 비용과 30~50% 오차 가능성을 응답에 명시

### 6-3. 웹훅 (Webhook) 지원
긴 작업(full-run) 완료 시 클라이언트가 폴링 대신 콜백 URL로 알림 받기.
- `POST /test-runs/full-run` 요청에 `webhook_url` 필드 추가
- 완료 시 `POST {webhook_url}` 으로 결과 전송

### 6-4. 실행 재시도
특정 TC만 재실행하는 엔드포인트.
- `POST /test-runs/{run_id}/rerun?tc_ids=id1,id2`
- 네트워크 오류 TC만 자동 재시도 옵션

### 6-5. JUnit XML 결과 내보내기
CI/CD 파이프라인에서 테스트 결과를 표준 포맷으로 소비할 수 있도록.
- `GET /test-runs/{run_id}/results?format=junit` → JUnit XML 반환
- GitHub Actions, Jenkins 등과 즉시 연동 가능

### 6-6. SSE 스트리밍 (Server-Sent Events)
현재 클라이언트는 `GET /test-runs/{run_id}` 를 반복 폴링해야 함.
- `GET /test-runs/{run_id}/stream` → SSE 스트림으로 실시간 진행 상황 push
- TC 실행 결과마다 `data: {...}` 이벤트 전송
- 폴링 오버헤드 제거, UI 반응성 향상

---

## 7. 보안 테스트 확장

현재 5종 (`auth_bypass`, `sql_injection`, `xss`, `idor`, `error_disclosure`) 외 추가 고려:

| 타입 | 설명 | 기대 결과 |
|------|------|-----------|
| `path_traversal` | `../../../etc/passwd` 경로 삽입 | 4xx = PASS |
| `ssrf` | `target` 파라미터에 내부 IP 삽입 | 4xx = PASS |
| `mass_assignment` | 요청 바디에 `role: admin` 등 권한 필드 삽입 | 4xx = PASS, 2xx = 취약점 |
| `rate_limit` | 동일 엔드포인트에 빠르게 N번 요청 | 429 응답 확인. **opt-in 전용** — 의도치 않은 DoS 방지를 위해 `GenerateConfig.enable_rate_limit_tests: bool = False` 로 명시 활성화 필요 |
| `csrf` | Origin/Referer 헤더 조작 | REST API + Bearer token 조합에선 CSRF 위협 낮음. 쿠키 기반 세션 API에만 의미 있음 — 스펙에서 쿠키 auth 감지 시에만 생성 |

---

## 우선순위 요약

| 중요도 | 항목 |
|--------|------|
| 🔴 High | 1-1 메모리 누수, 2-2 TC 생성 fallback, 1-2 배치 폴링 타임아웃 |
| 🟡 Medium | 2-1 local TC 커버리지, 4-1 Bearer token 재발급, 5-1 비용 추적, 6-1 페이지네이션, 6-4 실행 재시도 |
| 🟢 Low | 1-3 fallback 투명성, 2-3 TC 중복 제거, 3-1 대용량 PDF, 3-2 외부 $ref, 5-2 실행 메트릭, 6-2 비용 근사 추정, 6-3 웹훅, 7 보안 확장 |
| ⚫ 장기/재설계 | 3-3 GraphQL/gRPC (내부 모델 재설계 필요), 4-2 전처리 훅 (RCE 위험, 별도 아키텍처 필요) |
