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
10. [LLM Provider 선택](#10-llm-provider-선택)
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
  → parsed       (AI 모드만)          → generated                     → completed
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

전체 스펙을 Claude에게 보내서 **무엇을 테스트할지 계획**만 먼저 세운다. 실제 TC 값(path_params, body 등)은 이 단계에서 채우지 않는다.

**출력 — `TestPlan`:**

| 필드 | 내용 | 예시 |
|---|---|---|
| `individual_tests` | 엔드포인트별 TC 설명 목록 | `POST /users → ["happy path", "auth_bypass", "missing email"]` |
| `crud_scenarios` | 단일 도메인 시나리오 흐름 | `"회원가입 → 로그인 → 프로필 조회"` |
| `business_scenarios` | 복수 도메인 비즈니스 트랜잭션 | `"KYC → 계좌개설 → 주문 → 정산"` |

**Claude 호출 횟수 (순차 실행):**

| 단계 | 호출 횟수 | 배치 크기 |
|---|---|---|
| individual_tests | `ceil(N / 25)` 회 | 25 endpoints/배치 |
| CRUD 시나리오 | 1회 | 전체 스펙 |
| 도메인 분석 | 1회 | 전체 스펙 |
| 비즈니스 시나리오 | 1회 | 도메인 맵 |
| **합계 (130 endpoints)** | **6 + 3 = 9회** | |

결과 조회: `GET /test-runs/{run_id}/plan?method=GET&path=/users`

### Phase 2a: Generate — 개별 TC 생성 (`generating`)
**파일**: `tc_generator.py` (Claude) / `local_tc_generator.py` (local)

Phase 1의 계획을 체크리스트로 활용해서 **실제 값이 채워진 TC**를 생성한다.

**Claude 모드:**

| 항목 | 내용 |
|---|---|
| 배치 크기 | API 모드: 기본 3 endpoints/배치 (`max_tc_per_endpoint`에 따라 2/3/5 동적), CLI 모드: 25 endpoints/배치 |
| 호출 횟수 | API 모드 기준 `ceil(N / 3)` 근사 (130 endpoints → 약 **44회**) |
| 출력 | path_params, query_params, body, expected_status_codes 포함된 완성된 TC |
| 실패 시 | 해당 배치 자동으로 local 모드 fallback |

> **CLI 모드 주의**: subprocess 1회 타임아웃은 최대 300초이며, 배치 크기는 25다.
> **API 모드**는 배치 간 delay(`TC_BATCH_DELAY_SECONDS`)와 RPM 제한(`ANTHROPIC_RPM`) 영향을 받는다.

**파라미터 자동 보완 (Post-processing):**

AI가 TC를 생성한 직후 두 가지 자동 보완이 적용된다.

| 대상 | 로직 | 파일 |
|---|---|---|
| `path_params` | `{param}` 패턴을 path에서 추출 → 누락된 파라미터를 UUID placeholder로 채움 | `_fill_path_params()` |
| `query_params` | `EndpointSpec.parameters`에서 `required=True` query param을 찾아 → 누락 시 타입별 placeholder로 채움 (`string→"placeholder"`, `integer→0`, `boolean→true`) | `_fill_query_params()` |

이 보완 로직 덕분에 AI가 파라미터를 빠트려도 `Unresolved path params` 에러나 의도하지 않은 인증 오류 없이 실제 HTTP 요청이 발송된다.

**local 모드:**
- 규칙 기반으로 즉시 생성 (Claude 호출 없음)
- `strategy`별 보안 TC 쿼터: minimal=0, standard=1, exhaustive=5
- 전체 TC 한도: minimal=2, standard=4, exhaustive=12 (`max_tc_per_endpoint`로 override 가능)

결과 조회: `GET /test-runs/{run_id}/test-cases`

### Phase 2b: Generate — 시나리오 TC 생성 (`generating`)
**파일**: `tc_generator.py` → `generate_scenario_test_cases()`

Phase 1의 `crud_scenarios`와 `business_scenarios`를 받아 시나리오 단계별 TC를 생성한다.
- Anthropic API 모드: `asyncio.gather` 병렬 생성
- Claude CLI 모드: subprocess 부하를 줄이기 위해 순차 생성

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
- `network_error` 발생 시 해당 시나리오의 나머지 steps를 스킵

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
POST /api/v1/test-runs
Content-Type: multipart/form-data

spec_file: <openapi.yaml>

→ 202 Accepted
{ "run_id": "uuid", "status": "parsing" }
```

#### Step 2: TC 생성
```
POST /api/v1/test-runs/{run_id}/generate
Content-Type: multipart/form-data

phase1_provider: local | claude-api | claude-cli | gemini-api | gemini-cli | codex-api | codex-cli | ai-recom  (기본: local)
phase2_provider: local | claude-api | claude-cli | gemini-api | gemini-cli | codex-api | codex-cli | ai-recom  (기본: local)
phase3_provider: local | claude-api | claude-cli | gemini-api | gemini-cli | codex-api | codex-cli | ai-recom  (기본: local)
strategy: minimal | standard | exhaustive  (기본: standard)
auth_headers: '{"Authorization": "Bearer tok"}'  (선택)
max_tc_per_endpoint: 5            (선택, 1~100)
enable_rate_limit_tests: false    (선택, rate limit TC 생성 여부)

→ 202 Accepted
{ "run_id": "uuid", "status": "generating" }
```

#### Step 3: 실행
```
POST /api/v1/test-runs/{run_id}/execute
Content-Type: multipart/form-data

target_base_url: "http://localhost:8080"
timeout_seconds: 30               (선택, 기본 30)
webhook_url: "https://..."        (선택, 완료 시 결과 POST)

→ 202 Accepted
{ "run_id": "uuid", "status": "executing" }
```

#### 상태/결과 조회
```
GET /api/v1/test-runs/{run_id}                          # 전체 상태
GET /api/v1/test-runs/{run_id}/logs                     # 이벤트 로그
GET /api/v1/test-runs/{run_id}/plan                     # Phase 1 테스트 플랜 (AI 모드만, local 제외)
GET /api/v1/test-runs/{run_id}/estimate                 # 비용 예측 (AI 모드)
GET /api/v1/test-runs/{run_id}/test-cases               # 생성된 TC 목록 (?format=junit|md)
GET /api/v1/test-runs/{run_id}/test-cases/{tc_id}       # 단일 TC 조회
GET /api/v1/test-runs/{run_id}/test-cases/{tc_id}/expected-response  # 예상 응답
GET /api/v1/test-runs/{run_id}/results                  # 실행 결과 (파라미터 아래 참고)
GET /api/v1/test-runs/{run_id}/stream                   # SSE 스트림 (실시간 결과)
```

TC 목록 조회 파라미터:
```
GET /test-runs/{run_id}/test-cases
  ?format=junit           # JUnit XML 다운로드 (실행 전 TC — 모두 <skipped/> 처리, body/params를 <properties>로 포함)
  ?format=md              # Markdown 다운로드
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
  ?format=junit           # JUnit XML 다운로드 (실행 결과 포함 — passed/failed 반영)
  ?format=md              # Markdown 리포트 다운로드
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
내부에서는 파싱/생성/실행을 **순차 파이프라인**으로 처리한다.

```
POST /api/v1/test-runs/full-run
Content-Type: multipart/form-data

spec_file: <openapi.yaml>
target_base_url: "http://localhost:8080"
phase1_provider: local | claude-api | claude-cli | gemini-api | gemini-cli | codex-api | codex-cli | ai-recom  (기본: local)
phase2_provider: local | claude-api | claude-cli | gemini-api | gemini-cli | codex-api | codex-cli | ai-recom  (기본: local)
phase3_provider: local | claude-api | claude-cli | gemini-api | gemini-cli | codex-api | codex-cli | ai-recom  (기본: local)
strategy: minimal | standard | exhaustive  (기본: standard)
auth_headers: '{"Authorization": "Bearer tok"}'
max_tc_per_endpoint: 5
timeout_seconds: 30
postman_file: <collection.json>  (선택 — AI 컨텍스트용)
variables_file: <vars.json>      (선택 — Postman 변수 치환)
auth_context: <auth.md>          (선택 — 인증 메커니즘 보강 컨텍스트)
scenario_context: <scenario.md>  (선택 — 비즈니스/시나리오 보강 컨텍스트)

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
POST /api/v1/test-runs/import-postman
Content-Type: multipart/form-data

collection_file: <collection.json>
variables_file: <vars.json>   (선택)

→ 201 Created
{ "run_id": "uuid", "status": "generated", "test_case_count": 42 }
```
파싱 없이 바로 `generated` 상태. 이후 `/execute` 만 호출하면 된다.

#### Postman 임포트 + 즉시 실행
```
POST /api/v1/test-runs/postman-full-run
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
POST /api/v1/test-runs/{run_id}/rerun
Content-Type: multipart/form-data

target_base_url: "http://localhost:8080"
tc_ids: "uuid1,uuid2,uuid3"   (생략 시 network_error TC만 재실행)
timeout_seconds: 30
```

#### 취소
```
POST /api/v1/test-runs/{run_id}/cancel
```

#### SSE 스트림 (Server-Sent Events)
```
GET /api/v1/test-runs/{run_id}/stream

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

참고:
- Local 생성기는 `functional → security → boundary` 순서로 채운다.
- `rate_limit`은 security 빌더 중 하나이므로, strategy의 security quota/총 한도에 따라 실제 생성에서 제외될 수 있다.
- `max_tc_per_endpoint`를 직접 지정하면 security quota 보장이 비활성화된다.

---

### 5-2. AI 모드 (멀티 프로바이더)

Phase 1·2·3 각각에 사용할 LLM 프로바이더를 독립적으로 선택할 수 있다.

#### 지원 프로바이더

| 값 | 방식 | 비용 | 필요 조건 |
|---|---|---|---|
| `local` | 규칙 기반 (AI 없음) | **무료** | 없음 |
| `claude-api` | Anthropic API 직접 호출 | 토큰 과금 | `ANTHROPIC_API_KEY` |
| `claude-cli` | `claude` CLI subprocess | **무료** | Claude Pro/Max 구독 |
| `gemini-api` | Google Gemini API 직접 호출 | 토큰 과금 | `GEMINI_API_KEY` |
| `gemini-cli` | `gemini` CLI subprocess | **무료** | Gemini 구독 |
| `codex-api` | OpenAI API 직접 호출 | 토큰 과금 | `CODEX_API_KEY` |
| `codex-cli` | Codex CLI subprocess | **무료** | OpenAI Pro 구독 |
| `ai-recom` | 단계별 추천 프리셋 (아래 참조) | **무료** | 각 CLI 구독 |

#### ai-recom 프리셋 — 단계별 추천 구성

`ai-recom`을 지정하면 각 Phase에 최적의 무료 CLI 프로바이더가 자동으로 선택된다.

| Phase | 추천 프로바이더 | 이유 |
|---|---|---|
| Phase 0 (문서 파싱) | `gemini-api` | 긴 문서 처리·PDF 이해 성능 우수 |
| Phase 1 (플랜 수립) | `claude-cli` | 복잡한 스펙 분석·시나리오 도출에 Claude 추론 강점 |
| Phase 2 (TC 생성) | `gemini-cli` | 대량 배치 생성 속도 빠름 |
| Phase 3 (AI 검증) | `gemini-cli` | 고속 PASS/FAIL 판정 |

> Phase 0은 API 요청 파라미터(`phase0_provider`)가 아닌 환경변수 `PHASE0_PROVIDER=gemini-api`로 고정 설정한다.

`_RECOMMENDED` 매핑 (코드 기준):
```python
_RECOMMENDED = {
    "phase1_provider": "claude-cli",
    "phase2_provider": "gemini-cli",
    "phase3_provider": "gemini-cli",
}
```

#### Phase 1: 플랜 수립 (tc_planner.py)
전체 스펙을 AI에게 한 번에 보내고, 무엇을 테스트할지 **계획**을 먼저 세운다.
`phase1_provider=local`이면 이 단계를 건너뛴다 (TC는 스펙 구조만으로 자동 분류).

```
[AI] 전체 스펙 분석
  → 엔드포인트별 테스트 케이스 목록
  → 크로스 엔드포인트 시나리오 식별 (CRUD 흐름, 인증 흐름 등)
  → TestPlan 반환
```

결과는 `GET /test-runs/{run_id}/plan`으로 확인 가능.

#### Phase 2a: 개별 TC 생성 (tc_generator.py)
Phase 1 플랜을 가이드라인 삼아 AI가 실제 TC 값을 채운다.

```
엔드포인트를 소배치로 묶어서 AI에 전달
  → 각 배치마다 TC 목록(path_params, body, expected_status_codes 등) 반환
  → 배치 간 딜레이로 TPM 초과 방지

배치 크기 규칙:
- API 모드 (claude-api / gemini-api / codex-api): 기본 3
  `max_tc_per_endpoint` 지정 시 2/3/5로 동적 조정
- CLI 모드 (claude-cli / gemini-cli / codex-cli): 25 (subprocess 병렬 처리 우선)
```

AI 실패 시 해당 배치는 **자동으로 Local 모드로 fallback**.

#### Phase 2b: 시나리오 TC 생성 (tc_generator.py)
Phase 1의 `crud_scenarios`와 `business_scenarios`를 각각 실제 스텝 시퀀스로 변환한다.
- API 모드: `asyncio.gather` 병렬 생성
- CLI 모드: subprocess 부하를 줄이기 위해 순차 생성

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
- 시스템 프롬프트에 `cache_control: ephemeral` 붙여서 프롬프트 캐싱 활용 (claude-api만)
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
                              [AI 모드만]      │
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
  `running` 상태는 상태 타입에 남아 있으나, 현재 구현 파이프라인에서는 별도 전이 없이
  `parsing → parsed/analyzing/generating → generated → executing → completed`를 사용한다.
```

완료/실패/취소된 런은 `run_ttl_hours`(기본 24시간) 이후 메모리에서 자동 GC.

---

## 10. LLM Provider 선택

### 개요

각 파이프라인 Phase에 사용할 LLM 프로바이더를 **독립적으로** 지정할 수 있다.
Phase 1·2·3은 API 요청 파라미터(`phase1_provider`, `phase2_provider`, `phase3_provider`)로,
Phase 0은 환경변수 `PHASE0_PROVIDER`로 설정한다.

### 프로바이더 목록

| 값 | 방식 | 비용 | 필요 조건 |
|---|---|---|---|
| `local` | 규칙 기반 (AI 없음) | **무료** | 없음 |
| `claude-api` | Anthropic API | 토큰 과금 | `ANTHROPIC_API_KEY` |
| `claude-cli` | `claude` CLI subprocess | **무료** | Claude Pro/Max 구독 |
| `gemini-api` | Google Gemini API | 토큰 과금 | `GEMINI_API_KEY` |
| `gemini-cli` | `gemini` CLI subprocess | **무료** | Gemini 구독 |
| `codex-api` | OpenAI API | 토큰 과금 | `CODEX_API_KEY` |
| `codex-cli` | Codex CLI subprocess | **무료** | OpenAI Pro 구독 |
| `ai-recom` | Phase별 추천 무료 CLI 자동 선택 | **무료** | 각 CLI 구독 |

### ai-recom 프리셋

`ai-recom`을 지정하면 코드 내 `_RECOMMENDED` 매핑에 따라 Phase별로 최적의 무료 CLI 프로바이더가 자동 선택된다.

| Phase | 적용 프로바이더 | 이유 |
|---|---|---|
| Phase 0 (문서/PDF 파싱) | `gemini-api` | 긴 문서·PDF 이해 성능 우수. `.env`의 `PHASE0_PROVIDER=gemini-api`로 고정 |
| Phase 1 (테스트 플랜) | `claude-cli` | 복잡한 스펙 분석과 시나리오 도출에 Claude 추론 강점 |
| Phase 2 (TC 생성) | `gemini-cli` | 대량 배치 TC 생성 속도 우수 |
| Phase 3 (AI 검증) | `gemini-cli` | 고속 PASS/FAIL 판정 |

> Phase 0은 `GenerateConfig` 파라미터가 아닌 환경변수로 제어하므로, `ai-recom` 프리셋과 무관하게
> `.env`에서 `PHASE0_PROVIDER=gemini-api`를 별도로 설정해야 한다.

### claude-api / claude-cli 모드 — 모델별 용도

| 용도 | 모델 | 파일 |
|---|---|---|
| Phase 0: 스펙 파싱 (문서/PDF) | `CLAUDE_MODEL` (Sonnet 4.6) | `document_parser.py` |
| Phase 1: 플랜 수립 | `CLAUDE_MODEL` (Sonnet 4.6) | `tc_planner.py` |
| Phase 2a: TC 생성 | `CLAUDE_TC_MODEL` (Haiku 4.5) | `tc_generator.py` |
| Phase 2b: 시나리오 생성 | `CLAUDE_TC_MODEL` (Haiku 4.5) | `tc_generator.py` |
| Phase 3: AI 검증 | `CLAUDE_TC_MODEL` (Haiku 4.5) | `ai_validator.py` |

모든 API 호출은 `chat_with_tools`를 통해 **tool_use 강제**로 구조화된 JSON 응답만 받는다.

### CLI 모드 — 공통 동작 방식

구독 기반 CLI 프로바이더(`claude-cli`, `gemini-cli`, `codex-cli`)는 subprocess로 AI를 호출한다.

```
chat_with_tools() 호출
  → tool 스키마를 프롬프트에 내장
  → <cli> -p "...CRITICAL: Output ONLY JSON matching schema..." 실행
  → stdout 파싱 → JSON 추출 → 동일한 SimpleNamespace 반환
```

- JSON 마크다운 펜스 자동 제거 (```` ```json ... ``` ```` → raw JSON)
- 파싱 실패 시 최대 5회 재시도 (딜레이 2 × attempt초)
- 타임아웃 300초 per 호출
- PDF 파싱: CLI 모드에서는 `chat_with_tools_pdf` 미지원 → pypdf로 텍스트 추출 후 일반 경로 처리
- Phase 2 배치 크기: **25** (API 모드의 3보다 크게 설정 — subprocess 오버헤드 보상)

### Rate Limiting (API 모드만 적용)
```
슬라이딩 윈도우(_call_times deque)로 전역 직렬화
최소 간격 = 60초 윈도우 내 호출 수 < ANTHROPIC_RPM 유지 (기본 RPM=40)

재시도 (최대 5회):
  429 RateLimitError      → min(30 * 2^attempt, 300) + random(0,10)초 대기
  529 InternalServerError → min(15 * 2^attempt, 120) + random(0, 5)초 대기
```

Rate limiting은 `claude-api` 호출에만 적용된다. `gemini-api`, `codex-api`는 각 SDK가 자체 재시도를 처리한다.

### Mock 모드
`ANTHROPIC_API_KEY=mock-anything` 으로 실행하면 실제 호출 없이 테스트 가능.
모든 claude-api 호출이 미리 정의된 stub 응답을 반환한다.

---

## 11. 설정 레퍼런스

`.env` 파일로 설정. 모든 항목은 환경변수로도 override 가능.

### Anthropic (Claude)

| 키 | 기본값 | 설명 |
|---|---|---|
| `ANTHROPIC_API_KEY` | (필수) | `mock-*`으로 시작하면 mock 모드. CLI 전용 모드에선 임의값 가능 |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | 스펙 파싱(Phase 0) / 플랜 수립(Phase 1) 모델 |
| `CLAUDE_TC_MODEL` | `claude-haiku-4-5-20251001` | TC 생성(Phase 2) / AI 검증(Phase 3) 모델 |
| `ANTHROPIC_RPM` | `40` | 분당 API 호출 상한 (0=무제한, claude-api 모드만) |
| `TC_BATCH_DELAY_SECONDS` | `10` | TC 생성 배치 간 딜레이 (API 모드만) |

### Gemini

| 키 | 기본값 | 설명 |
|---|---|---|
| `GEMINI_API_KEY` | `""` | Gemini API 키 (`gemini-api` 모드에서 필요) |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini API 호출 시 사용 모델 |

### OpenAI / Codex

| 키 | 기본값 | 설명 |
|---|---|---|
| `CODEX_API_KEY` | `""` | OpenAI API 키 (`codex-api` 모드에서 필요) |
| `CODEX_MODEL` | `gpt-4o-mini` | Codex API 호출 시 사용 모델 |

### 글로벌 LLM Provider (레거시 서버 기본값)

| 키 | 기본값 | 설명 |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | 서버 기본 프로바이더. API 요청의 `phase*_provider`로 override됨 |
| `PHASE0_PROVIDER` | `""` | Phase 0(문서 파싱) 프로바이더. 빈 값이면 `LLM_PROVIDER` 상속 |
| `PHASE1_PROVIDER` | `""` | Phase 1 서버 기본값. 빈 값이면 `LLM_PROVIDER` 상속 |
| `PHASE2A_PROVIDER` | `""` | Phase 2a 서버 기본값 |
| `PHASE2B_PROVIDER` | `""` | Phase 2b 서버 기본값 |
| `PHASE3_PROVIDER` | `""` | Phase 3 서버 기본값 |

> API 요청의 `phase1_provider` / `phase2_provider` / `phase3_provider` 파라미터가 환경변수보다 우선한다.

### 실행 / 공통

| 키 | 기본값 | 설명 |
|---|---|---|
| `MAX_CONCURRENT_REQUESTS` | `10` | 동시 httpx 요청 수 |
| `REQUEST_TIMEOUT_SECONDS` | `30` | 요청당 타임아웃(초) |
| `RUN_TTL_HOURS` | `24` | 완료 런 메모리 보존 시간 |
| `AI_VALIDATE_TIMEOUT_SECONDS` | `120` | AI 검증 타임아웃(초) |
| `MAX_RESPONSE_BODY_BYTES` | `10240` | 응답 바디 최대 저장 크기 (10KB) |
| `MODEL_INPUT_PRICE_PER_MTOK` | `0.80` | 입력 토큰 단가 ($/1M, Haiku 기준) |
| `MODEL_OUTPUT_PRICE_PER_MTOK` | `4.00` | 출력 토큰 단가 ($/1M) |
| `MODEL_CACHE_CREATION_PRICE_PER_MTOK` | `1.00` | 캐시 생성 토큰 단가 |
| `MODEL_CACHE_READ_PRICE_PER_MTOK` | `0.08` | 캐시 읽기 토큰 단가 |

### Phase 2 배치 크기 비교

| Provider 유형 | 배치 크기 | 130 endpoints 기준 호출 수 |
|---|---|---|
| API 모드 (`claude-api` / `gemini-api` / `codex-api`) | 3 | ~44회 |
| CLI 모드 (`claude-cli` / `gemini-cli` / `codex-cli`) | 25 | ~6회 (subprocess 오버헤드 보상) |

---

## 부록: 빠른 시작 예시

### 1. OpenAPI 스펙 → 풀런 (Local TC, 무료, 즉시)
```bash
curl -X POST http://localhost:8000/api/v1/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "strategy=standard"
# phase*_provider 생략 시 모두 local(규칙 기반)으로 동작
```

### 2. 상태 폴링
```bash
curl http://localhost:8000/api/v1/test-runs/{run_id}
```

### 3. 결과 확인
```bash
# 실패 TC만
curl "http://localhost:8000/api/v1/test-runs/{run_id}/results?passed=false"

# 트랙별 필터
curl "http://localhost:8000/api/v1/test-runs/{run_id}/results?track=crud"

# JUnit XML 다운로드 (CI/CD 연동)
curl "http://localhost:8000/api/v1/test-runs/{run_id}/results?format=junit" -o results.xml
```

### 4. Claude API 모드 (AI 생성, 인증 포함)
```bash
curl -X POST http://localhost:8000/api/v1/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "phase1_provider=claude-api" \
  -F "phase2_provider=claude-api" \
  -F "phase3_provider=claude-api" \
  -F "strategy=standard" \
  -F 'auth_headers={"Authorization": "Bearer eyJ..."}'
```

### 5. ai-recom 프리셋 (무료 CLI 자동 선택)
```bash
# .env 에 아래 추가:
# PHASE0_PROVIDER=gemini-api   ← 문서 파싱용 (ai-recom 외 Phase 0 설정)
# GEMINI_API_KEY=...
# ANTHROPIC_API_KEY=dummy      ← claude-cli 사용 시 임의값 가능

curl -X POST http://localhost:8000/api/v1/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "phase1_provider=ai-recom" \
  -F "phase2_provider=ai-recom" \
  -F "phase3_provider=ai-recom" \
  -F "strategy=standard"
# → Phase 1: claude-cli / Phase 2: gemini-cli / Phase 3: gemini-cli
```

### 6. 혼합 구성 (Phase별 최적 프로바이더 직접 지정)
```bash
curl -X POST http://localhost:8000/api/v1/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "phase1_provider=claude-cli" \
  -F "phase2_provider=gemini-cli" \
  -F "phase3_provider=gemini-cli" \
  -F "strategy=exhaustive" \
  -F 'auth_headers={"Authorization": "Bearer eyJ..."}'
```

### 7. Postman 컬렉션으로 즉시 실행
```bash
curl -X POST http://localhost:8000/api/v1/test-runs/postman-full-run \
  -F "collection_file=@collection.json" \
  -F "target_base_url=http://localhost:8080" \
  -F "variables_file=@vars.json"
```

### 8. Gemini API 단독 사용
```bash
# .env: GEMINI_API_KEY=...

curl -X POST http://localhost:8000/api/v1/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "phase1_provider=gemini-api" \
  -F "phase2_provider=gemini-api" \
  -F "phase3_provider=gemini-api" \
  -F "strategy=standard"
```
