# blind-test-bot

API 스펙 파일만 있으면 테스트 케이스를 자동으로 생성하고 실행해주는 FastAPI 서비스.

"Blind" = 실제 API 구현에 접근하지 않고, **공개 스펙만 보고** 테스트를 생성합니다.

```
스펙 업로드 → 파싱 → [AI 계획 수립] → TC 생성 → 실행 → 결과
```

**지원 스펙 포맷:** OpenAPI/Swagger YAML·JSON · 텍스트/PDF 문서 · Postman 컬렉션

---

## 빠른 시작

```bash
pip install -r requirements.txt
```

**.env 파일 생성:**
```env
# Claude API 사용 (토큰 과금)
ANTHROPIC_API_KEY=sk-ant-...
LLM_PROVIDER=anthropic

# 또는 Claude CLI 사용 (Pro 구독, 추가 비용 없음)
ANTHROPIC_API_KEY=dummy
LLM_PROVIDER=claude-cli
```

```bash
# 서버 실행 (Swagger UI: http://localhost:8000/docs)
uvicorn app.main:app --reload

# mock 모드 (API 호출 없이 테스트)
ANTHROPIC_API_KEY=mock-anything uvicorn app.main:app --reload
```

---

## 사용 예시

### 한 번에 실행 (권장)

```bash
curl -X POST http://localhost:8000/api/v1/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "generator=claude" \
  -F "strategy=standard" \
  -F 'auth_headers={"Authorization": "Bearer <token>"}'
```

### 단계별 실행

```bash
# 1. 스펙 업로드
curl -X POST http://localhost:8000/api/v1/test-runs \
  -F "spec_file=@openapi.yaml"
# → {"run_id": "abc123", "status": "parsing"}

# 2. TC 생성
curl -X POST http://localhost:8000/api/v1/test-runs/abc123/generate \
  -F "generator=claude" \
  -F "strategy=standard" \
  -F 'auth_headers={"Authorization": "Bearer <token>"}'

# 3. 실행
curl -X POST http://localhost:8000/api/v1/test-runs/abc123/execute \
  -F "target_base_url=http://localhost:8080"

# 4. 결과 조회
curl http://localhost:8000/api/v1/test-runs/abc123/results
```

### 결과 실시간 스트리밍

```bash
curl -N http://localhost:8000/api/v1/test-runs/abc123/stream
```

### JUnit XML 출력 (CI 연동)

```bash
curl "http://localhost:8000/api/v1/test-runs/abc123/results?format=junit" > results.xml
```

### Postman 연동

```bash
# 컬렉션 직접 임포트 → 즉시 실행
curl -X POST http://localhost:8000/api/v1/test-runs/postman-full-run \
  -F "collection_file=@collection.json" \
  -F "target_base_url=http://localhost:8080" \
  -F "variables_file=@variables.json"
```

---

## TC 생성 방식

| 방식 | 설명 | 비용 | 속도 |
|------|------|------|------|
| `local` (기본) | 규칙 기반 자동 생성 | 무료 | 즉시 |
| `claude` | AI가 엔드포인트를 분석해 생성 | 과금 또는 Pro 구독 | 느림 |

### Strategy

| 전략 | TC 수 (per endpoint) |
|------|----------------------|
| `minimal` | 2 |
| `standard` | 4 (기본값) |
| `exhaustive` | 12 |

---

## 보안 테스트 (자동 생성)

| 타입 | 설명 | PASS 조건 |
|------|------|-----------|
| `auth_bypass` | 인증 없이 접근 시도 | 4xx |
| `sql_injection` | SQL 인젝션 페이로드 | 4xx (500 = 취약점) |
| `xss` | XSS 페이로드 삽입 | 4xx |
| `idor` | 다른 유저 리소스 접근 | 4xx (2xx = 취약점) |
| `error_disclosure` | 스택 트레이스 노출 유도 | 상세 500 없음 |
| `path_traversal` | `../` 경로 주입 | 400/403/404 |
| `ssrf` | 내부 IP 주입 | 400/403 |
| `mass_assignment` | `role: admin` 주입 | 400/403/422 |

---

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/v1/test-runs` | 스펙 업로드, 파싱 시작 |
| POST | `/api/v1/test-runs/{id}/generate` | TC 생성 |
| POST | `/api/v1/test-runs/{id}/execute` | TC 실행 |
| POST | `/api/v1/test-runs/{id}/rerun` | 네트워크 오류 TC 재실행 |
| POST | `/api/v1/test-runs/{id}/cancel` | 실행 취소 |
| POST | `/api/v1/test-runs/full-run` | 파싱 + 생성 + 실행 한 번에 |
| POST | `/api/v1/test-runs/import-postman` | Postman 컬렉션 임포트 |
| POST | `/api/v1/test-runs/postman-full-run` | Postman 임포트 + 즉시 실행 |
| GET | `/api/v1/test-runs/{id}` | 상태 조회 |
| GET | `/api/v1/test-runs/{id}/logs` | 이벤트 로그 |
| GET | `/api/v1/test-runs/{id}/plan` | Phase 1 테스트 계획 |
| GET | `/api/v1/test-runs/{id}/estimate` | 비용 예측 |
| GET | `/api/v1/test-runs/{id}/test-cases` | 생성된 TC 목록 |
| GET | `/api/v1/test-runs/{id}/test-cases/{tc_id}` | 단일 TC 조회 |
| GET | `/api/v1/test-runs/{id}/results` | 실행 결과 |
| GET | `/api/v1/test-runs/{id}/stream` | SSE 실시간 스트림 |

전체 API 문서: `http://localhost:8000/docs`

---

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | 필수 | `mock-`로 시작하면 mock 모드 |
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `claude-cli` |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | 스펙 파싱·플랜 수립 모델 |
| `CLAUDE_TC_MODEL` | `claude-haiku-4-5-20251001` | TC 생성·검증 모델 |
| `ANTHROPIC_RPM` | `40` | 분당 API 요청 수 제한 |
| `MAX_CONCURRENT_REQUESTS` | `10` | 동시 실행 요청 수 |
| `REQUEST_TIMEOUT_SECONDS` | `30` | 요청당 타임아웃 (초) |
| `RUN_TTL_HOURS` | `24` | 완료 런 메모리 보존 시간 |

---

## 테스트 실행

```bash
python3 -m pytest tests/
python3 -m pytest tests/unit/test_swagger_parser.py      # 특정 파일
python3 -m pytest tests/ --cov=app --cov-report=term-missing  # 커버리지
```
