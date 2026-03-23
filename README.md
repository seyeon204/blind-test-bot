# blind-test-bot

API 스펙 파일만 있으면 테스트 케이스를 자동으로 생성하고 실행해주는 FastAPI 서비스.

"Blind" = 실제 API에 접근하지 않고 스펙만 보고 테스트를 생성합니다.

## 동작 방식

```
spec 파일 업로드 → 엔드포인트 파싱 → 테스트 케이스 생성 → 실행 → 결과 리포트
```

**지원 스펙 포맷:** OpenAPI/Swagger YAML·JSON, 일반 문서(텍스트/PDF), Postman 컬렉션

## 빠른 시작

```bash
pip install -r requirements.txt

# .env 파일 생성
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# 서버 실행 (Swagger UI: http://localhost:8000/docs)
uvicorn app.main:app --reload
```

API 토큰 없이 테스트하려면:
```bash
ANTHROPIC_API_KEY=mock-anything uvicorn app.main:app --reload
```

## 사용 예시

### 1단계씩 실행

```bash
# 1. 스펙 업로드 → run_id 발급
curl -X POST http://localhost:8000/test-runs \
  -F "spec_file=@openapi.yaml"
# → {"run_id": "abc123", "status": "parsing"}

# 2. 테스트 케이스 생성 (local: 무료 규칙 기반 / claude: AI 생성)
curl -X POST http://localhost:8000/test-runs/abc123/generate \
  -F "generator=claude" \
  -F "strategy=standard" \
  -F 'auth_headers={"Authorization": "Bearer <token>"}'

# 3. 실행
curl -X POST http://localhost:8000/test-runs/abc123/execute \
  -F "target_base_url=http://localhost:8080"

# 4. 결과 조회
curl http://localhost:8000/test-runs/abc123/results
```

### 한 번에 실행 (full-run)

```bash
curl -X POST http://localhost:8000/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "target_base_url=http://localhost:8080" \
  -F "generator=claude" \
  -F "strategy=exhaustive" \
  -F 'auth_headers={"Authorization": "Bearer <token>"}'
```

### JUnit XML 출력 (CI 연동)

```bash
curl "http://localhost:8000/test-runs/abc123/results?format=junit" > results.xml
```

### 실행 결과 실시간 스트리밍

```bash
curl -N http://localhost:8000/test-runs/abc123/stream
```

## 테스트 생성 전략

| 전략 | 설명 |
|------|------|
| `minimal` | 핵심 케이스만 (happy path + 주요 에러) |
| `standard` | 일반적인 커버리지 (기본값) |
| `exhaustive` | 모든 엣지 케이스 + 보안 테스트 전부 |

## 보안 테스트

Claude 모드에서는 아래 보안 테스트를 자동으로 생성합니다:

| 타입 | 설명 | 판정 기준 |
|------|------|-----------|
| `auth_bypass` | 인증 없이 접근 시도 | 4xx = PASS, 2xx = 취약점 |
| `sql_injection` | SQL 인젝션 페이로드 | 500 = 가능성 있음 |
| `xss` | XSS 페이로드 삽입 | 4xx = PASS |
| `idor` | 다른 유저 리소스 접근 | 2xx = IDOR 취약점 |
| `error_disclosure` | 스택 트레이스 노출 유도 | 상세 500 = 취약점 |

## Postman 연동

**컬렉션 임포트** (기존 Postman 테스트 → 바로 실행):
```bash
curl -X POST http://localhost:8000/test-runs/import-postman \
  -F "collection_file=@collection.json" \
  -F "variables_file=@variables.json"
```

**컨텍스트로 활용** (Claude가 Postman의 실제 auth 헤더·예시 바디를 참고해 더 정확한 TC 생성):
```bash
curl -X POST http://localhost:8000/test-runs/full-run \
  -F "spec_file=@openapi.yaml" \
  -F "postman_file=@collection.json" \
  -F "target_base_url=http://localhost:8080"
```

## 주요 API

| Method | Path | 설명 |
|--------|------|------|
| POST | `/test-runs` | 스펙 업로드, 파싱 시작 |
| POST | `/test-runs/{id}/generate` | TC 생성 |
| POST | `/test-runs/{id}/execute` | TC 실행 |
| POST | `/test-runs/{id}/cancel` | 실행 취소 |
| POST | `/test-runs/{id}/rerun` | 특정 TC 또는 네트워크 오류 TC 재실행 |
| POST | `/test-runs/full-run` | 파싱 + 생성 + 실행 한 번에 |
| GET | `/test-runs/{id}` | 상태 조회 |
| GET | `/test-runs/{id}/results` | 실행 결과 (`?passed=`, `?format=junit`) |
| GET | `/test-runs/{id}/plan` | Phase 1 테스트 계획 |
| GET | `/test-runs/{id}/estimate` | Claude TC 생성 토큰 비용 예측 |
| GET | `/test-runs/{id}/stream` | SSE 실시간 스트림 |

전체 API 문서: `http://localhost:8000/docs`

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | 필수 | `mock-`로 시작하면 mock 모드 |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | 스펙 파싱용 모델 |
| `CLAUDE_TC_MODEL` | `claude-haiku-4-5-20251001` | TC 생성·검증용 모델 (저렴) |
| `ANTHROPIC_RPM` | `40` | 분당 API 요청 수 제한 |
| `TC_BATCH_DELAY_SECONDS` | `10` | TC 생성 배치 간 대기 시간 |
| `MAX_CONCURRENT_REQUESTS` | `10` | 동시 실행 요청 수 |
| `REQUEST_TIMEOUT_SECONDS` | `30` | 요청당 타임아웃 |

## 테스트 실행

```bash
pip install -r requirements-dev.txt

python3 -m pytest tests/
python3 -m pytest tests/unit/test_swagger_parser.py        # 특정 파일
python3 -m pytest tests/ --cov=app --cov-report=term-missing  # 커버리지
```
