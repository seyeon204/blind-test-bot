# Blind Test Bot — 서비스 명세서

## 목차

1. [개요](#1-개요)
2. [핵심 개념: "블라인드" 테스트란](#2-핵심-개념-블라인드-테스트란)
3. [파이프라인 상세](#3-파이프라인-상세)
4. [서비스 호출 방법](#4-서비스-호출-방법)
5. [TC 생성 방식](#5-tc-생성-방식)
6. [실행 엔진](#6-실행-엔진)
7. [검증 방식](#7-검증-방식)
8. [보안 테스트](#8-보안-테스트)
9. [상태 흐름](#9-상태-흐름)
10. [Claude 모델 활용](#10-claude-모델-활용)
11. [설정 레퍼런스](#11-설정-레퍼런스)

---

## 1. 개요

Blind Test Bot은 **API 명세서(스펙)만 보고** 자동으로 테스트 케이스(TC)를 생성하고 실행하는 FastAPI 서비스다.
운영 중인 API 코드나 내부 구현에 접근하지 않고, 공개된 스펙 파일(OpenAPI, Swagger, PDF 문서, Postman 컬렉션)만으로 테스트를 완성한다.

```
스펙 파일 → [파싱] → [TC 생성] → [실행] → [검증] → 결과
```

서버 실행 후 Swagger UI는 `http://localhost:8000/docs` 에서 바로 확인할 수 있다.

---

## 2. 핵심 개념: "블라인드" 테스트란

**블라인드(blind)**는 테스트 케이스를 만들 때 실제 API가 어떻게 구현됐는지 *전혀 모르는 상태*에서 생성한다는 뜻이다.

| 일반 테스트 | 블라인드 테스트 |
|---|---|
| 코드를 직접 읽고 TC 작성 | 스펙 문서만 보고 TC 작성 |
| 구현 디테일에 의존 | 공개 계약(스펙)에만 의존 |
| 화이트박스 | 블랙박스 |

이 접근법의 장점:
- **스펙 위반 탐지**: 스펙대로 요청했는데 서버가 다르게 응답하면 그것 자체가 버그
- **외부 공격자 시각**: 실제 공격자와 동일한 정보(공개 스펙)만 가지고 보안 테스트 수행
- **자동화**: 스펙만 업로드하면 TC가 자동으로 나오므로 테스트 작성 공수 0

---

## 3. 파이프라인 상세

모든 테스트 실행은 아래 5단계를 순서대로 거친다.

```
┌──────────┐   ┌──────────┐   ┌───────────────────────────────┐   ┌──────────┐
│  PARSE   │──▶│ ANALYZE  │──▶│           GENERATE            │──▶│ EXECUTE  │
│ 스펙 파싱 │   │ 테스트 계획│   │  Phase2a: 개별 TC  Phase2b: 시나리오│   │ 실행+검증 │
└──────────┘   └──────────┘   └───────────────────────────────┘   └──────────┘
  parsing        analyzing           generating                      executing
  → parsed       (claude만)          → generated                     → completed
```

### Phase 0: Parse — 스펙 파싱 (`parsing` → `parsed`)
**파일**: `spec_parser.py` → `swagger_parser.py` / `document_parser.py` / `postman_parser.py`

스펙 파일을 읽어서 **엔드포인트 목록**을 추출한다.

지원 포맷:
| 포맷 | 파서 | 비고 |
|---|---|---|
| OpenAPI 3.x YAML/JSON | `swagger_parser.py` | 파라미터, 스키마, 보안 스킴 전부 추출 |
| Swagger 2.0 YAML/JSON | `swagger_parser.py` | 동일 |
| 텍스트/마크다운 API 문서 | `document_parser.py` | Claude가 NLP로 엔드포인트 추출 |
| PDF API 문서 | `document_parser.py` | Claude Document API 활용 |
| Postman Collection v2.x | `postman_parser.py` | 요청 예제 그대로 TC로 변환 |

파싱 결과는 `ParsedSpec` 객체로 정규화된다:
```
ParsedSpec
├── source_format: "openapi" | "swagger" | "document"
├── base_url: "https://api.example.com"
└── endpoints: [EndpointSpec, ...]
    ├── method: "POST"
    ├── path: "/users/{id}"
    ├── parameters: [ParameterSpec, ...]
    ├── request_body_schema: { JSON Schema }
    ├── expected_responses: {"200": {...}, "404": {...}}
    └── security_schemes: ["bearerAuth"]
```

보안 스킴 결정 우선순위:
1. 엔드포인트 수준 `security` 명시 → 그대로 사용
2. 스펙 최상위 `security` 전역 설정 → 상속
3. 둘 다 없고 `securitySchemes`만 선언됐을 경우 → 스킴 이름 추론
   *(단, 일부 엔드포인트에 명시적 `security`가 있으면 추론 생략 — 의도적 공개 엔드포인트 존중)*

### Phase 1: Analyze — 테스트 계획 수립 (`analyzing`)
**파일**: `tc_planner.py` — **Claude 모드 전용**

전체 스펙을 Claude(Sonnet)에게 보내서 **무엇을 테스트할지 계획**을 먼저 세운다.

출력 — `TestPlan`:
- **`individual_tests`**: 엔드포인트별 TC 목록 초안 (happy path, auth_bypass, SQL injection 등)
- **`crud_scenarios`**: 단일 도메인 통합 시나리오 (CRUD lifecycle, 인증 플로우, 의존성 체인)
- **`business_scenarios`**: 복수 도메인을 가로지르는 실제 비즈니스 트랜잭션 (e.g. KYC → 계좌개설 → 주문 → 정산)

Phase 1은 항상 4개의 독립적인 Claude 호출로 구성된다 (순차 실행):
1. **individual_tests 배치** — 25개씩 묶어 배치 처리 (대용량 스펙 대응)
2. **CRUD 시나리오** — 전체 스펙으로 단일 호출. 단일 도메인 흐름 식별 (CRUD lifecycle, 인증 흐름)
3. **도메인 분석** — 전체 스펙으로 단일 호출. API를 비즈니스 도메인으로 분해
4. **비즈니스 시나리오** — 도메인 맵 기반 단일 호출. 크로스 도메인 통합 시나리오 생성

결과 조회: `GET /test-runs/{run_id}/plan?method=GET&path=/users`

### Phase 2a: Generate — 개별 TC 생성 (`generating`)
**파일**: `tc_generator.py` (Claude) / `local_tc_generator.py` (local)

Phase 1 계획을 체크리스트로 활용해서 실제 TC를 생성한다.

**Claude 모드**:
- 3개 엔드포인트씩 배치로 묶어 Claude(Haiku)에게 요청
- Phase 1 계획이 있으면 `planned_cases`를 체크리스트로 첨부 → Claude가 해당 케이스를 채워서 반환
- 결과: 구체적인 path params, query params, request body, expected_status_codes 포함

**local 모드**:
- 규칙 기반으로 즉시 생성 (API 호출 없음)
- `strategy`별 보안 TC 쿼터 보장: minimal=0, standard=1, exhaustive=5
- 스키마에서 타입/범위/enum 읽어 경계값 자동 생성

결과 조회: `GET /test-runs/{run_id}/test-cases`

### Phase 2b: Generate — 시나리오 TC 생성 (`generating`)
**파일**: `tc_generator.py` → `generate_scenario_test_cases()`

Phase 1의 `crud_scenarios`와 `business_scenarios`를 **동시에(병렬로)** 받아 시나리오 단계별 TC를 생성한다.
두 트랙이 `asyncio.gather`로 병렬 실행되므로 시나리오 수가 많아도 생성 시간이 단축된다.

예시: "회원가입 → 로그인 → 보호된 리소스 접근" 시나리오
```
Step 1: POST /auth/register  → 201  → extract: userId
Step 2: POST /auth/login     → 200  → extract: token
Step 3: GET  /profile/{id}   → 200  (token 자동 주입)
```

각 step은 `extract` 명세를 통해 앞 단계 응답값을 다음 단계에 자동으로 주입한다.

### Phase 3: Execute — 실행 + 검증 (`executing` → `completed`)
**파일**: `executor.py` + `ai_validator.py`

**개별 TC 실행**:
- httpx로 실제 HTTP 요청 전송 (최대 `MAX_CONCURRENT_REQUESTS`개 동시)
- 실행 완료 후 **AI Validation**: Claude(Haiku)가 PASS/FAIL 판정
  - 보안 TC와 일반 TC를 분리해서 별도 Claude 호출 (판정 기준이 반대라 혼용 방지)
  - Claude 호출 실패 시 heuristic validator로 자동 폴백

**시나리오 TC 실행** (단일도메인 + 크로스도메인 병렬):
```
개별 TC 실행 완료
        ↓
  asyncio.gather(
    _run_scenario_list(crud_scenarios),      ← 단일도메인 순차 실행
    _run_scenario_list(business_scenarios),  ← 크로스도메인 순차 실행
  )
```
- 각 트랙 내부는 **순차 실행** (앞 단계 응답값을 다음 단계에 주입해야 하므로)
- 두 트랙은 **서로 독립적이므로 병렬**로 실행
- 한 step이 실패하면 해당 시나리오의 나머지 steps만 스킵

결과는 트랙별로 분리되어 저장된다:
- `crud_scenario_results` — 단일도메인 시나리오 결과
- `business_scenario_results` — 크로스도메인 시나리오 결과

결과 조회: `GET /test-runs/{run_id}/results` (`validation_mode: "ai" | "heuristic"` 필드로 판정 방식 확인 가능)

---

## 4. 서비스 호출 방법

### 4-1. 단계별 호출 (Step-by-Step)

스펙 파싱 → TC 생성 → 실행을 각각 별도 API로 호출하는 방식이다.
중간 단계 결과(TC 목록, 플랜 등)를 확인하거나 세밀하게 제어할 때 사용한다.

#### Step 1: 스펙 업로드 & 파싱 시작
```
POST /test-runs
Content-Type: multipart/form-data

spec_file: <openapi.yaml>

→ 202 Accepted
{ "run_id": "uuid", "status": "parsing" }
```

#### Step 2: TC 생성
```
POST /test-runs/{run_id}/generate
Content-Type: multipart/form-data

generator: local | claude         (기본: local)
strategy: minimal | standard | exhaustive  (기본: standard)
auth_headers: '{"Authorization": "Bearer tok"}'  (선택)
max_tc_per_endpoint: 5            (선택, 1~100)
enable_rate_limit_tests: false    (선택, rate limit TC 생성 여부)

→ 202 Accepted
{ "run_id": "uuid", "status": "generating" }
```

#### Step 3: 실행
```
POST /test-runs/{run_id}/execute
Content-Type: multipart/form-data

target_base_url: "http://localhost:8080"
timeout_seconds: 30               (선택, 기본 30)
webhook_url: "https://..."        (선택, 완료 시 결과 POST)

→ 202 Accepted
{ "run_id": "uuid", "status": "executing" }
```

#### 상태/결과 조회
```
GET /test-runs/{run_id}             # 전체 상태
GET /test-runs/{run_id}/logs        # 이벤트 로그 (실시간)
GET /test-runs/{run_id}/test-cases  # 생성된 TC 목록
GET /test-runs/{run_id}/plan        # Phase 1 테스트 플랜 (claude 모드만)
GET /test-runs/{run_id}/results     # 실행 결과 (파라미터 아래 참고)
GET /test-runs/{run_id}/stream      # SSE 스트림 (실시간 결과)
GET /test-runs/{run_id}/estimate    # 비용 예측 (claude 모드)
```

결과 조회 파라미터:
```
GET /test-runs/{run_id}/results
  ?passed=true            # 통과한 개별 TC만
  ?passed=false           # 실패한 개별 TC만
  ?track=individual       # 개별 TC 결과만 (기본: 전체 포함)
  ?track=crud             # 단일도메인 시나리오 결과만
  ?track=business         # 크로스도메인 시나리오 결과만
  ?page=1&page_size=100   # 페이지네이션 (개별 TC 대상)
  ?format=junit           # JUnit XML 포맷 다운로드 (개별 TC 대상)
```

응답 구조:
```json
{
  "status": "completed",
  "total_summary": {
    "individual": { "total": N, "passed": N, "failed": N },
    "crud":       { "total": N, "passed": N, "failed": N },
    "business":   { "total": N, "passed": N, "failed": N }
  },
  "result_count": N,
  "results": [...],                    // 개별 TC 결과 (페이지네이션 적용)
  "crud_scenario_results": [...],      // 단일도메인 시나리오 결과
  "business_scenario_results": [...]   // 크로스도메인 시나리오 결과
}
```

---

### 4-2. 원샷 풀런 (Full-Run)

파싱 → TC 생성 → 실행을 한 번의 API 호출로 처리한다.
내부에서는 생성과 실행을 **동시에** 스트리밍으로 처리한다 (엔드포인트 하나씩 생성되자마자 바로 실행).

```
POST /test-runs/full-run
Content-Type: multipart/form-data

spec_file: <openapi.yaml>
target_base_url: "http://localhost:8080"
generator: local | claude
strategy: minimal | standard | exhaustive
auth_headers: '{"Authorization": "Bearer tok"}'
max_tc_per_endpoint: 5
timeout_seconds: 30
postman_file: <collection.json>  (선택 — Claude 컨텍스트용)
variables_file: <vars.json>      (선택 — Postman 변수 치환)

→ 202 Accepted
{ "run_id": "uuid", "status": "parsing" }
```

풀런 내부 흐름:
```
_parse_pipeline()                    # Phase 0: 스펙 파싱
    ↓
_generate_pipeline()                 # Phase 1 (Analyze) + Phase 2a/2b (Generate)
    ├── plan_test_cases()            # Claude 모드: tc_planner
    │     → TestPlan(individual_tests, crud_scenarios, business_scenarios)
    ├── generate_test_cases()        # Phase 2a: 엔드포인트별 개별 TC 생성 (배치)
    └── asyncio.gather(              # Phase 2b: 시나리오 TC 병렬 생성
          generate_scenario_test_cases(crud_scenarios),
          generate_scenario_test_cases(business_scenarios),
        )
    ↓
_execute_pipeline()                  # Phase 3: 실행 + AI 검증
    ├── 개별 TC 청크 실행
    └── asyncio.gather(              # 시나리오 병렬 실행
          _run_scenario_list(crud_scenarios),
          _run_scenario_list(business_scenarios),
        )
```

파싱 → 생성 → 실행을 **순차적으로** 처리한다. 각 단계가 완전히 끝난 후 다음 단계로 넘어간다.

---

### 4-3. Postman 전용 호출

#### Postman 컬렉션을 TC로 직접 임포트
```
POST /test-runs/import-postman
Content-Type: multipart/form-data

collection_file: <collection.json>
variables_file: <vars.json>   (선택)

→ 201 Created
{ "run_id": "uuid", "status": "generated", "test_case_count": 42 }
```
파싱 없이 바로 `generated` 상태. 이후 `/execute` 만 호출하면 된다.

#### Postman 임포트 + 즉시 실행
```
POST /test-runs/postman-full-run
Content-Type: multipart/form-data

collection_file: <collection.json>
target_base_url: "http://localhost:8080"
timeout_seconds: 30
variables_file: <vars.json>   (선택)
```

---

### 4-4. 보조 기능

#### 재실행
```
POST /test-runs/{run_id}/rerun
Content-Type: multipart/form-data

target_base_url: "http://localhost:8080"
tc_ids: "uuid1,uuid2,uuid3"   (생략 시 network_error TC만 재실행)
timeout_seconds: 30
```

#### 취소
```
POST /test-runs/{run_id}/cancel
```

#### SSE 스트림 (Server-Sent Events)
```
GET /test-runs/{run_id}/stream

→ text/event-stream
data: {"test_case_id": "...", "passed": true, ...}
data: {"test_case_id": "...", "passed": false, ...}
event: done
data: {"status": "completed"}
```

---

## 5. TC 생성 방식

### 5-1. Local 모드 (기본, 무료)

규칙 기반으로 TC를 즉시 생성한다. API 호출 없음, 비용 없음.

각 엔드포인트에 대해 아래 빌더를 순서대로 적용하며 `strategy` 한도까지만 생성:

| 빌더 | 설명 |
|---|---|
| `_happy_path` | 정상 요청 — 스펙의 required 필드 전부 채워서 전송 |
| `_auth_bypass` | 인증 헤더 없이 전송 (security_schemes 있는 경우만) |
| `_not_found` | 경로 파라미터를 "없을 법한 ID"로 교체 |
| `_missing_required_field` | required 필드 하나 제거 |
| `_wrong_type` | integer 필드에 문자열 넣기 |
| `_sql_injection` | string 필드에 `' OR '1'='1` 주입 |
| `_xss` | string 필드에 `<script>alert(1)</script>` 주입 |
| `_idor` | ID 파라미터를 다른 유저 ID로 교체 |
| `_error_disclosure` | body를 null로 보내 verbose 500 유도 |
| `_path_traversal` | string 파라미터에 `../../../etc/passwd` 주입 |
| `_ssrf` | url/target/redirect 파라미터에 내부 IP 주입 |
| `_mass_assignment` | body에 `role: admin, isAdmin: true` 주입 |
| `_boundary_values` | 빈 문자열, 음수, 0, 10001자 문자열 (복수 TC) |
| `_rate_limit` | 동일 요청 20번 연속 전송 (opt-in, `enable_rate_limit_tests=true`) |

**Strategy별 TC 한도** (per endpoint):

| strategy | 한도 |
|---|---|
| minimal | 2 |
| standard | 4 |
| exhaustive | 12 |

`max_tc_per_endpoint` 파라미터로 override 가능.

---

### 5-2. Claude 모드 (AI 생성)

Claude API를 호출해서 더 정교한 TC를 생성한다. 3단계로 나뉜다.

#### Phase 1: 플랜 수립 (tc_planner.py)
전체 스펙을 Claude Sonnet에게 한 번에 보내고, 무엇을 테스트할지 **계획**을 먼저 세운다.

```
[Claude Sonnet] 전체 스펙 분석
  → 엔드포인트별 테스트 케이스 목록
  → 크로스 엔드포인트 시나리오 식별 (CRUD 흐름, 인증 흐름 등)
  → TestPlan 반환
```

결과는 `GET /test-runs/{run_id}/plan`으로 확인 가능.

#### Phase 2a: 개별 TC 생성 (tc_generator.py)
Phase 1 플랜을 가이드라인 삼아 Claude Haiku가 실제 TC 값을 채운다.

```
엔드포인트를 3개씩 배치로 묶어서 Haiku에 전달
  → 각 배치마다 TC 목록(path_params, body, expected_status_codes 등) 반환
  → 배치 간 딜레이로 TPM 초과 방지
```

Claude 실패 시 해당 배치는 **자동으로 Local 모드로 fallback**.

#### Phase 2b: 시나리오 TC 생성 (tc_generator.py)
Phase 1의 `crud_scenarios`와 `business_scenarios`를 각각 실제 스텝 시퀀스로 변환한다.
두 트랙이 `asyncio.gather`로 **병렬 생성**된다.

```
asyncio.gather(
  generate_scenario_test_cases(plan.crud_scenarios, ...),     → _crud_scenarios_internal
  generate_scenario_test_cases(plan.business_scenarios, ...),  → _business_scenarios_internal
)
```

변환 예시:
```
PlannedScenario: ["POST /users", "GET /users/{id}", "DELETE /users/{id}"]
  ↓
TestScenario: [
  Step 0: POST /users → body: {name: "testuser"} → extract: {userId: "id"}
  Step 1: GET /users/{{userId}} → path_params: {id: "{{userId}}"}
  Step 2: DELETE /users/{{userId}}
]
```

각 스텝에서 응답값을 추출해(`extract`) 다음 스텝에 주입(`{{varName}}`).

**비용 최적화 설계:**
- 시스템 프롬프트에 `cache_control: ephemeral` 붙여서 프롬프트 캐싱 활용
- 배치 딜레이: 기본 10초, 성공 3회 연속 시 자동으로 절반으로 단축, 429 응답 시 2배로 증가
- 중복 TC 자동 제거: (method, path, description) 기준

---

## 6. 실행 엔진

실행 엔진(`executor.py`)은 httpx 기반 비동기 클라이언트다.

### 동시 실행
```python
MAX_CONCURRENT_REQUESTS = 10  # 동시 실행 상한
semaphore = asyncio.Semaphore(10)
```
모든 TC를 동시에 fire하되 세마포어로 동시 요청 수를 제한한다.

### 요청 구성
```
URL:     base_url + path (path_params 치환)
Method:  tc.endpoint_method
Headers: tc.headers
Params:  tc.query_params
Body:    tc.body (JSON)
```

경로 파라미터 미치환 시 (`{param}` 잔존) 네트워크 요청 없이 `network_error`로 처리.

### 응답 처리
```
응답 크기 > max_response_body_bytes (기본 10KB)
  → { __truncated__: true, __preview__: "...(500자)" }

Content-Type: image/*, application/pdf, application/octet-*
  → { __binary__: true, content_type: "...", size_bytes: N }

정상 JSON 응답
  → response.json()

나머지
  → response.text
```

### 시나리오 실행
실행 순서: **개별 TC 완료 → crud 트랙과 business 트랙을 병렬 실행**

```
1. 개별 TC 청크 실행 (동시, 세마포어 제한)
2. asyncio.gather(
     _run_scenario_list(crud_scenarios),     ← 트랙 내부는 순차
     _run_scenario_list(business_scenarios),  ← 트랙 내부는 순차
   )
```

각 트랙 내부는 순차 실행이다. 앞 스텝에서 추출한 값을 `context`에 쌓고 다음 스텝의 `{{varName}}` 템플릿에 주입한다. 중간에 `network_error` 발생 시 이후 스텝은 건너뛴다.

### Rate Limit TC 실행
`repeat_count > 1`인 TC (rate_limit 타입)는 동일 요청을 N번 연속으로 보내서, 그 중 하나라도 429를 받으면 PASS로 처리한다.

---

## 7. 검증 방식

실행 결과를 pass/fail로 판정하는 방법은 두 가지다.

### 7-1. AI 검증 (기본)

실행 결과를 배치로 묶어 Claude Haiku에게 한 번에 판정을 맡긴다.
`validation_mode: "ai"`로 표시된다.

판정 기준 (Claude에게 주입된 규칙):
```
Happy path          → 2xx면 PASS
Negative test       → 4xx면 PASS (서버가 잘 걸러낸 것)
auth_bypass         → 4xx면 PASS, 2xx면 취약점 발견
Non-existent (404)  → 404 또는 403이면 PASS
sql_injection / xss → 4xx 또는 2xx(sanitised)면 PASS, 500이면 FAIL
idor                → 4xx면 PASS, 2xx면 취약점 발견
error_disclosure    → 500인데 stack trace 없으면 PASS

관대함 규칙:
  400 ≈ 422 (둘 다 클라이언트 에러로 동등 취급)
  401 ≈ 403 (둘 다 "보호됨"으로 동등 취급)
  201 / 202 / 204 모두 2xx 성공으로 인정
```

타임아웃(`ai_validate_timeout_seconds`, 기본 120초) 초과 또는 Claude 오류 시 → 자동으로 휴리스틱 fallback.

**Adaptive Thinking**: `claude_tc_model`이 Sonnet 4.6 / Opus 4.x 계열이면 `thinking: {type: "adaptive"}`를 활성화해 복잡한 보안 판정에 추론 단계를 더한다.

### 7-2. 휴리스틱 검증 (fallback)

`validation_mode: "heuristic"`으로 표시된다.

판정 순서:
1. `network_error` 있으면 즉시 FAIL
2. 응답 상태코드가 `expected_status_codes`에 포함되는지 확인
3. `auth_bypass` 타입이면 4xx 응답은 무조건 PASS
4. `expected_body_schema` 있으면 JSON Schema 검증 (jsonschema)
5. `expected_body_contains` 있으면 shallow key-value 비교

---

## 8. 보안 테스트

보안 테스트가 포함된 TC는 `security_test_type` 필드가 설정된다.
실행 후 취약점이 감지되면 `vulnerabilities` 목록에 추가된다.

| security_test_type | 생성 조건 | PASS 조건 | 취약점 트리거 |
|---|---|---|---|
| `auth_bypass` | security_schemes 있는 엔드포인트 | 4xx | 2xx (인증 없이 접근 성공) |
| `sql_injection` | string 필드 있는 body | 4xx 또는 200(sanitised) | 500 (쿼리 에러 노출) |
| `xss` | string 필드 있는 body | 4xx 또는 200(sanitised) | 500 |
| `idor` | 경로에 ID 파라미터 | 4xx | 2xx (크로스 유저 접근 성공) |
| `error_disclosure` | body 있는 POST/PUT/PATCH | 4xx 또는 500(sensitive 패턴 없음) | 500 + stack trace/SQL 포함 |
| `path_traversal` | string 타입 path/query 파라미터 | 400/403/404 | (AI 판정) |
| `ssrf` | url/uri/target/redirect 이름 파라미터 | 400/403 | (AI 판정) |
| `mass_assignment` | body 있는 POST/PUT/PATCH | 400/403/422 | (AI 판정) |
| `rate_limit` | 모든 엔드포인트 (opt-in) | 429 수신 | 한 번도 429 없음 |

`error_disclosure` 취약점 감지에 쓰이는 패턴:
```
traceback, stack trace, exception, syntaxerror, typeerror, valueerror,
sql syntax, pg::, mysql, sqlite, ora-, internal server error,
debug, secret, password, private_key
```

취약점은 severity 레벨로 분류된다:
- `critical`: auth_bypass (인증 우회)
- `high`: sql_injection, xss, idor
- `medium`: error_disclosure

---

## 9. 상태 흐름

```
                     [parse]
  POST /test-runs ──▶ parsing ──▶ parsed ──▶ (generate)
                                              │
                              [claude 모드만]  │
                              analyzing ◀─────┤
                                   │          │
                                   ▼          │
                              generating ◀───┘
                                   │
                              generated
                                   │
                     [execute]     │
                     executing ◀──┘
                          │
                       completed

  실패:   failed    (어느 단계에서든)
  취소:   cancelled (task.cancel())
  풀런:   running   (generate + execute 순차 진행 중)
```

완료/실패/취소된 런은 `run_ttl_hours`(기본 24시간) 이후 메모리에서 자동 GC.

---

## 10. Claude 모델 활용

### LLM Provider 선택

`LLM_PROVIDER` 설정으로 Claude 호출 방식을 전환한다.

| `LLM_PROVIDER` | 방식 | 비용 | 조건 |
|---|---|---|---|
| `anthropic` (기본) | Anthropic API 직접 호출 | 토큰 과금 | API 키 필요 |
| `claude-cli` | `claude` CLI subprocess 호출 | **$0** | Claude Pro/Max 구독 필요 |

### anthropic 모드 — 모델별 용도

| 용도 | 모델 | 파일 |
|---|---|---|
| 스펙 파싱 (문서/PDF) | `claude_model` (Sonnet 4.6) | `document_parser.py` |
| Phase 1 플랜 수립 | `claude_model` (Sonnet 4.6) | `tc_planner.py` |
| Phase 2a TC 생성 | `claude_tc_model` (Haiku 4.5) | `tc_generator.py` |
| Phase 2b 시나리오 생성 | `claude_tc_model` (Haiku 4.5) | `tc_generator.py` |
| AI 검증 | `claude_tc_model` (Haiku 4.5) | `ai_validator.py` |

모든 호출은 `chat_with_tools`를 통해 **tool_use 강제**로 구조화된 JSON 응답만 받는다.

### claude-cli 모드 — 동작 방식

Claude Pro/Max 구독이 있으면 API 키 없이 CLI subprocess로 모든 호출을 대체한다.

```
chat_with_tools() 호출
  → tool 스키마를 프롬프트에 내장
  → claude -p "...CRITICAL: Output ONLY JSON matching schema..." 실행
  → stdout 파싱 → JSON 추출 → 동일한 SimpleNamespace 반환
```

- JSON 마크다운 펜스 자동 제거 (```` ```json ... ``` ```` → raw JSON)
- 파싱 실패 시 최대 5회 재시도 (딜레이 2 * attempt초)
- 타임아웃 120초 per 호출
- PDF 파싱: `chat_with_tools_pdf` 미지원 → pypdf로 텍스트 추출 후 일반 `chat_with_tools` 경로로 처리

### Rate Limiting (anthropic 모드만 적용)
```
슬라이딩 윈도우(_call_times deque)로 전역 직렬화
최소 간격 = 60초 윈도우 내 호출 수 < ANTHROPIC_RPM 유지 (기본 RPM=40)

재시도 (최대 5회):
  429 RateLimitError      → min(30 * 2^attempt, 300) + random(0,10)초 대기
  529 InternalServerError → min(15 * 2^attempt, 120) + random(0, 5)초 대기
```

### Mock 모드
`ANTHROPIC_API_KEY=mock-anything` 으로 실행하면 실제 호출 없이 테스트 가능.
모든 Claude 호출이 미리 정의된 stub 응답을 반환한다.

---

## 11. 설정 레퍼런스

`.env` 파일로 설정. 모든 항목은 환경변수로도 override 가능.

| 키 | 기본값 | 설명 |
|---|---|---|
| `ANTHROPIC_API_KEY` | (필수) | `mock-*`으로 시작하면 mock 모드. `claude-cli` 모드에선 임의값 가능 |
| `LLM_PROVIDER` | `anthropic` | `anthropic` (API 키 과금) \| `claude-cli` (Pro 구독, $0) |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | 스펙 파싱 / 플랜 수립 모델 (anthropic 모드만) |
| `CLAUDE_TC_MODEL` | `claude-haiku-4-5-20251001` | TC 생성 / AI 검증 모델 (anthropic 모드만) |
| `ANTHROPIC_RPM` | `40` | 분당 API 호출 상한 (0=무제한, anthropic 모드만) |
| `TC_BATCH_DELAY_SECONDS` | `10` | TC 생성 배치 간 딜레이(초) |
| `MAX_CONCURRENT_REQUESTS` | `10` | 동시 httpx 요청 수 |
| `REQUEST_TIMEOUT_SECONDS` | `30` | 요청당 타임아웃(초) |
| `RUN_TTL_HOURS` | `24` | 완료 런 메모리 보존 시간 |
| `AI_VALIDATE_TIMEOUT_SECONDS` | `120` | AI 검증 타임아웃(초) |
| `MAX_RESPONSE_BODY_BYTES` | `10240` | 응답 바디 최대 저장 크기 (10KB) |
| `MODEL_INPUT_PRICE_PER_MTOK` | `0.80` | 입력 토큰 단가 ($/1M, Haiku 기준) |
| `MODEL_OUTPUT_PRICE_PER_MTOK` | `4.00` | 출력 토큰 단가 ($/1M) |
| `MODEL_CACHE_CREATION_PRICE_PER_MTOK` | `1.00` | 캐시 생성 토큰 단가 |
| `MODEL_CACHE_READ_PRICE_PER_MTOK` | `0.08` | 캐시 읽기 토큰 단가 |

---

## 부록: 빠른 시작 예시

### 1. OpenAPI 스펙 → 풀런 (Local TC, 무료)
```bash
curl -X POST http://localhost:8000/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "generator=local" \
  -F "strategy=standard"
```

### 2. 상태 폴링
```bash
curl http://localhost:8000/test-runs/{run_id}
```

### 3. 결과 확인
```bash
# 실패 TC만
curl "http://localhost:8000/test-runs/{run_id}/results?passed=false"

# JUnit XML 다운로드 (CI/CD 연동)
curl "http://localhost:8000/test-runs/{run_id}/results?format=junit" -o results.xml
```

### 4. Claude 모드로 더 정교한 테스트 (인증 포함)
```bash
curl -X POST http://localhost:8000/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "generator=claude" \
  -F "strategy=exhaustive" \
  -F 'auth_headers={"Authorization": "Bearer eyJ..."}'
```

### 5. Postman 컬렉션으로 즉시 실행
```bash
curl -X POST http://localhost:8000/test-runs/postman-full-run \
  -F "collection_file=@collection.json" \
  -F "target_base_url=http://localhost:8080" \
  -F "variables_file=@vars.json"
```

### 6. Claude CLI 모드로 실행 (Pro 구독, API 비용 $0)
```bash
# .env
# LLM_PROVIDER=claude-cli
# ANTHROPIC_API_KEY=mock-not-needed

uvicorn app.main:app --reload

curl -X POST http://localhost:8000/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "generator=claude" \
  -F "strategy=standard"
```
