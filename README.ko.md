# spec2openapi

[English README](README.md)

레거시 API 명세(SOAP/WSDL, Swagger 2.0)를 **FastMCP 변환이 보장되는 OpenAPI 3.x 스펙**으로 변환하는 파이썬 라이브러리.

기여 방법은 [CONTRIBUTING.md](CONTRIBUTING.md), 행동 강령은 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), 보안 제보는 [SECURITY.md](SECURITY.md)를 참고. 라이선스는 [Apache-2.0](LICENSE).

입력에 따라 산출물의 성격이 다른데, 이 구분이 중요하다.

- **Swagger 2.0 → 평범한 표준 OpenAPI 3.x 문서.** 경로가 곧 실제 REST 엔드포인트다. 어떤 OpenAPI 런타임(FastMCP나 직접 만든 httpx 서버)에 넣어도 **런타임을 전혀 바꾸지 않고** 실제 API를 호출한다.
- **WSDL → OpenAPI 3.x + `x-soap` 계약.** 생성된 `/operations/...` 경로는 **실제 REST 엔드포인트가 아니다.** 각 tool 호출을 SOAP envelope으로 직렬화하고, SOAP 엔드포인트로 POST한 뒤, XML 응답을 다시 JSON으로 역직렬화하는 계층이 필요하다. 이 계층은 표준 OpenAPI 런타임에는 없으며, **`[mcp]` extra의 SOAP 브리지**가 바로 그 구현이다. SOAP 변환 스펙을 일반 OpenAPI/httpx 런타임으로 서빙하면 SOAP 서버에 JSON을 그대로 POST하게 되어 모든 호출이 실패한다.

정리하면, spec2openapi는 **Swagger 2.0에 대해서는 순수 변환기**이고, **SOAP에 대해서는 변환기이자 런타임 계약(참조 브리지 포함)**이다.

```
Swagger 2.0 ──(convert)──> 표준 REST OpenAPI ──> 어떤 OpenAPI 런타임이든 그대로 서빙

WSDL ──(convert)──> OpenAPI + x-soap 계약 ──> [mcp] 브리지(필수)로만 실제 SOAP 호출
```

## 설치

**Python 3.10 이상**이 필요하다. (더 낮은 버전에서는 버전 하한에 걸려 모든
릴리즈가 걸러지므로 pip이 `No matching distribution found`라고만 보고한다.)

```bash
pip install spec2openapi          # 변환기 + CLI (zeep, lxml, PyYAML)
pip install "spec2openapi[mcp]"   # + SOAP 브리지·런타임 — SOAP 스펙 서빙에 필수

# 소스 체크아웃에서 개발용 설치 (테스트/검증 도구 포함)
pip install -e ".[dev]"
```

> 코어만 설치해도 모든 스펙을 **변환**할 수 있으며, **Swagger 변환 결과(REST)** 스펙은 직접 만든 런타임으로 서빙할 수 있다. `[mcp]` extra는 **SOAP 변환 스펙을 서빙**할 때만 필요하다(JSON tool 호출을 SOAP envelope으로 바꾸는 브리지를 제공한다).

## CLI

```bash
# WSDL 내용 확인 (오퍼레이션/헤더/fault/스타일)
spec2openapi inspect https://legacy-host/OrderService?wsdl

# WSDL 변환 (zip 번들, stdin('-')도 입력 가능)
spec2openapi convert https://legacy-host/OrderService?wsdl -o orders.openapi.yaml
spec2openapi convert service.wsdl --openapi-version 3.1 --format json
spec2openapi convert vendor-bundle.zip -o orders.openapi.yaml
spec2openapi convert - < service.wsdl

# Swagger 2.0 -> OpenAPI 3.x 업그레이드 (FastMCP는 3.x만 지원)
# --strict: 가정/손실 변환이 하나라도 필요하면 목록과 함께 실패
spec2openapi upgrade swagger2.json -o service.openapi.yaml   # 파일 또는 URL

# FastMCP 변환 가능성 검증 (스펙 정적 검사 + openapi-spec-validator
# + FastMCP.from_openapi 라운드트립으로 tool 생성까지 확인)
spec2openapi validate orders.openapi.yaml
spec2openapi validate orders.openapi.yaml --format json   # 기계가 읽는 검증 보고서

# 참조 MCP 런타임 ([mcp] extra 필요)
spec2openapi serve orders.openapi.yaml --transport http --port 8000
```

convert 입력: 파일 경로, http(s) URL, zip 번들(WSDL+XSD 묶음, 안전 검사 포함), `-`(stdin). convert 옵션: `--openapi-version 3.0|3.1`(기본값 `3.0`, 출력 스펙에는 `openapi: 3.0.3`으로 기록), `--service`/`--port-name`, `--prefer-soap12`, `--base-path`, `--title`, `--strict`(미지원 오퍼레이션을 건너뛰지 않고 실패 처리), `--forbid-external`(원격 wsdl:/xsd: import를 가져오지 않음. 신뢰할 수 없는 WSDL을 변환할 때 권장하며, 로컬 상대경로 import는 그대로 동작한다), `--huge-tree`(초대형 WSDL을 위해 libxml2 제한 해제).

모든 XML 파싱은 DTD 로딩·엔티티 해석·파서 차원의 네트워크 접근을 차단한다. 상세한 보안 노트는 [SECURITY.md](SECURITY.md) 참조.

## 라이브러리 API

```python
import spec2openapi
from spec2openapi import ConversionError

# Swagger 2.0 -> OpenAPI dict (입력은 파일 경로 또는 http(s) URL)
try:
    legacy = spec2openapi.load_spec("swagger2.json")
    spec = spec2openapi.convert_swagger(legacy, openapi_version="3.1")
except ConversionError as exc:      # 모든 실패 경로가 이 예외를 던진다
    raise SystemExit(f"변환 실패: {exc}")

# 변환기가 가정했거나 번역하지 못한 내용은 문서마다 기록된다
report = spec.get("x-s2o", {})
report.get("assumptions", [])       # 예: "missing consumes -> application/json"
report.get("lossy", [])             # 예: "collectionFormat 'tsv' preserved as x-"
# 추측을 허용하면 안 되는 파이프라인: convert_swagger(legacy, strict=True)

# FastMCP 호환 계약을 함수로 검사 (빈 리스트 == 준비 완료)
problems = spec2openapi.check_fastmcp_ready(spec)

# 구조화 검증: 위 계약에 x-soap 계약 검사, openapi-spec-validator,
# FastMCP 인메모리 라운드트립까지 더한 보고서 (Swagger/WSDL 변환 스펙 공통)
result = spec2openapi.verify(spec)
result.ok          # 실패한 체크 없음
result.complete    # 건너뛴 체크 없음 (딥 의존성이 설치되어 실제로 돌았음)
result.to_dict()   # 체크별 규범 근거 인용이 담긴 JSON 직렬화 가능 보고서

# WSDL -> OpenAPI dict (zeep은 import 시점이 아니라 첫 SOAP 사용 때 로드)
spec = spec2openapi.convert_wsdl(
    "https://legacy-host/OrderService?wsdl",
    forbid_external=True,           # 신뢰할 수 없는 WSDL의 원격 import 차단
)

# WSDL 입력은 유연하다: 경로/URL, zip 번들, 문서 본문 자체,
# 상대 import를 내부에서 해소하는 인메모리 멀티파일 번들 전부 가능
spec = spec2openapi.convert_wsdl(content=wsdl_bytes)
spec = spec2openapi.convert_wsdl(files={"service.wsdl": wsdl_bytes,
                                        "types.xsd": xsd_bytes})

text = spec2openapi.dump_spec(spec)              # yaml (또는 fmt="json")

# [mcp] extra 설치 시: 참조 런타임
mcp = spec2openapi.from_openapi_spec(spec)       # x-soap 감지 시 SOAP 브리지 장착
mcp.run(transport="http", host="0.0.0.0", port=8000)
```

공개 API는 정확히 `spec2openapi.__all__`이며(타입 힌트 제공, PEP 561), 그 밖의
이름은 내부 구현으로 예고 없이 바뀔 수 있다. 모든 진입점은 실패를
`ConversionError`(`ValueError`의 서브클래스)로 보고한다. 반환된 문서는 입력
매핑과 하위 구조를 공유할 수 있으므로(`example`/`default`/`enum`, `x-*` 값은
deep copy하지 않음), 입력을 계속 쓰면서 결과를 수정하려면 `copy.deepcopy`를
먼저 적용한다.

## 지원 범위

| 영역 | 지원 |
|---|---|
| 바인딩 스타일 | document/literal(wrapped), rpc/literal. rpc/encoded는 스킵 후 `x-soap.skippedOperations`에 기록 |
| SOAP 버전 | 1.1, 1.2 (듀얼 포트 자동 중복 제거, `--prefer-soap12`) |
| 타입 | 중첩 complexType($ref), 배열(maxOccurs→array/minItems/maxItems), attribute, nillable, anyType, 상속(complexContent extension은 평탄화), simpleContent(값+attribute), choice(멤버를 optional로 처리 + `x-soap-choice`), substitution group(head 참조를 멤버별 자기서술 branch의 `oneOf`로 전개 + `x-soap-substitution`), default 값 |
| XSD 파셋 | enumeration, pattern, length/minLength/maxLength, min/maxInclusive, min/maxExclusive, fractionDigits(→multipleOf). xsd:import/include된 외부 스키마 포함. 결정적 `example` 값 부여(첫 enumeration 값, 날짜/시간 표기 예시 상수 — 합성 없음) |
| 문서화 | wsdl:documentation(서비스/오퍼레이션), xsd:annotation/documentation(타입/엘리먼트) → description으로 이관 (LLM tool 설명 품질에 직결) |
| WSDL 구조 | 다중 service/port, operationId 충돌 시 서비스명 접두 부여, soap:header(`x-soap.headers` + 스키마 컴포넌트), wsdl:fault(`x-soap.faults` + 스키마 컴포넌트), one-way 오퍼레이션 |
| 미지원 | rpc/encoded, MTOM/첨부, WS-Policy/WS-Addressing (substitution group의 blocking/final 제약은 무시) |

## Swagger 2.0 업그레이드와 정보 부족 처리

3.x는 2.0의 상위집합이라 변환은 대부분 기계적이다: `host`+`basePath`+`schemes`→`servers`, body 파라미터→`requestBody`, formData→form/multipart requestBody, 파라미터 타입 필드→`schema` 래핑, `collectionFormat`→`style`/`explode`, `definitions`→`components/schemas`($ref 전부 재작성), 전역 parameters/responses→components(body 파라미터는 `requestBodies`로), `securityDefinitions`→`securitySchemes`(oauth2 flow 이름 매핑 포함), `type: file`→`string`/`binary`, `x-nullable`→`nullable`, discriminator 문자열→객체.

여기에 실전 문서를 상대로 한 하드닝이 더해져 있다: 임의 위치를 가리키는 깊은
로컬 `$ref`는 컴포넌트로 hoist하고, dangling ref와 중복 파라미터는 무해화하며,
타입이 어긋난 default는 코어스하고, 널리 쓰이는 벤더 확장(`x-example`,
`x-oneOf`, `x-anyOf`)은 네이티브 키워드로 승격한다. 또한 `--openapi-version
3.1`은 버전 문자열만 바꾸는 게 아니라 JSON Schema 2020-12 스타일로의 시맨틱
변환이다(`nullable`→`type` 배열, boolean `exclusiveMinimum`/`exclusiveMaximum`→
숫자 경계).

정보가 부족한 경우에는 다음 3단계 정책을 따른다.

1. 합의된 기본값을 결정적으로 적용하고, 그 내용을 모두 루트 `x-s2o.assumptions`에 기록한다. `consumes`/`produces`가 없으면 `application/json`으로 가정하고, `operationId`가 없으면 `{method}_{path}` 규칙으로 생성하며(FastMCP tool 이름에 필수), `host`가 없으면 상대 서버 `/`를 쓰고 런타임 엔드포인트 오버라이드에 맡기며, `schemes`가 없으면 https로 가정한다.
2. 대응되는 구조가 없는 경우(collectionFormat `tsv` 등)는 버리지 않고 `x-` 확장으로 보존하며 `x-s2o.lossy`에 기록한다.
3. 마지막 관문은 `spec2openapi validate`다. 가정이 들어간 스펙이라도 FastMCP 라운드트립으로 tool 생성까지 실제로 확인한다. tool을 만드는 데 필요한 것은 paths와 스키마뿐이고 서버·인증은 런타임의 몫이므로, 가정 때문에 tool 생성이 막히는 일은 없다.

`upgrade` 실행 시 assumptions/lossy 목록이 stderr로 출력되고, `validate`/`serve`는 Swagger 2.0 입력을 자동 감지해 메모리에서 업그레이드한다.

## FastMCP 호환 보장

이 프로젝트가 보장하는 계약은 이렇다. 생성된 스펙은 `FastMCP.from_openapi()`를 통과해 오퍼레이션 수만큼의 MCP tool을 만들어낸다.

- FastMCP는 tool 이름을 `[A-Za-z0-9_]`로 정규화하므로, operationId를 처음부터 그 알파벳으로 생성한다(중복 없음, 64자 이하). 따라서 *tool 이름 == operationId*가 항상 성립한다.
- `spec2openapi validate <spec>`이 정적 검사 + 실제 FastMCP 라운드트립으로 이를 확인한다. 같은 검증을 코드에서 쓰려면 `spec2openapi.verify(spec)` — 체크별 상태와 규범 근거(SEP-986 등)가 담긴 구조화 보고서를 반환하며, `validate --format json`이 동일 보고서를 CLI로 노출한다.
- 테스트 스위트가 모든 픽스처 WSDL에 대해 3.0/3.1 두 버전 모두 라운드트립을 검증한다.
- description, enum, pattern, min/max 등은 tool 스키마까지 그대로 전달되어 LLM의 인자 생성 품질을 높인다.

## LLM에게 실리는 표면 최소화 (선택 기능)

스펙을 MCP로 서빙하면 모델은 OpenAPI 문서를 읽지 않는다. FastMCP가 보내는 것은 tool 목록이며, 각 tool은 `name`, `description`, `inputSchema`, 그리고 FastMCP 3.x부터는 2xx 응답 스키마로 만든 `outputSchema`를 담는다. 나머지(`info`, 참조되지 않는 컴포넌트, 에러 응답, 미디어타입 example, `$ref` 구조)는 서버에 남는다. 그래서 두 가지 비용이 생긴다. 스키마 내용은 tool 스키마로 그대로 복사되므로 기계 배선용 벤더 확장이 모든 `tools/list` 응답에서 컨텍스트 토큰을 낭비하고, tool 형태에 자리가 없는 정보(에러 응답, request/response example)는 사람이 써 두었어도 모델에게 전달되지 않는다.

`minify_for_mcp`는 이 두 방향을 모두 다루는 선택적 후처리 단계다. 변환 파이프라인 자체는 그대로다.

```python
spec = spec2openapi.convert_swagger(legacy)          # 또는 convert_wsdl(...)
spec = spec2openapi.minify_for_mcp(spec, enrich=("errors", "examples"))
```

- **기본 동작**: 보존 목록에 없는 벤더 `x-*` 확장을 스키마 서브트리에서 제거한다(FastMCP가 tool 페이로드로 복사하는 위치들). 런타임과 독자가 필요로 하는 것은 전부 살아남는다. SOAP 브리지가 읽는 `x-soap*`와 `xml` 어노테이션, `x-s2o`, `x-fastmcp-*`, 이 프로젝트의 보존용 확장(`x-pattern`, `x-collectionFormat`), 문서성 확장(`x-enum-varnames`, `x-example` 등)이 그렇다. 자체 확장은 `keep_extensions=("x-acme-*",)`로 지킨다.
- **`enrich`**: `"errors"`는 에러 응답을 `Errors: 404 (not found); ...` 형태의 한 줄로 description에 접어 넣는다(에러 응답은 그 외의 방법으로는 모델에게 전달되지 않는다). `"examples"`는 request/response 페이로드 example을 `Example request: {...}` 줄로 접고, 파라미터 수준 `example` 값을 스키마 안으로 옮겨 FastMCP가 실제로 보여주는 위치에 놓는다. 문서에 이미 있는 사실만 사용하며 아무것도 지어내지 않고, 다시 실행해도 중복으로 접히지 않는다.
- **opt-in 축소**: `max_description=N`은 페이로드에 도달하는 description 길이를 제한한다(잘린 텍스트는 `…`로 표시). `drop_value_examples=True`는 스키마 안의 example을 제거하는데, example은 보통 모델의 인자 형식을 돕는 정보이므로 효과를 측정한 뒤 켜는 것을 권한다.

어떤 옵션 조합에서도 결과물은 유효한 OpenAPI 문서이고 `check_fastmcp_ready`를 통과하며 동일하게 서빙된다. 어떤 옵션도 호출 표면을 건드리지 않으므로 SOAP 브리지 envelope도 바이트 단위로 같다. 제거되거나 접힌 내용은 `x-s2o.minify`에 요약된다. 경량화는 단방향이므로 결과는 별도 파일로 저장하고 원본을 보관한다.

### 대형 스펙: route map으로 tool 개수 제어

`minify_for_mcp`는 tool 하나하나를 다듬지만, 대형 서비스에서 지배적인 컨텍스트 비용은 tool 개수다. operation마다 tool이 하나씩 생기고, MCP 클라이언트는 `tools/list` 결과 전체를 매 요청마다 모델 컨텍스트에 싣는다. `examples/` 실측 기준 tool 하나의 페이로드가 대략 0.3~1.7KB이므로 operation 100개짜리 서비스는 대화가 시작되기도 전에 수십 KB를 싣게 되고, 선택지가 100개인 모델은 5개인 모델보다 tool을 잘못 고르는 일도 잦다. 실제 에이전트에게 필요한 것은 보통 그중 몇 개뿐이다.

어떤 operation을 tool로 만들지 고르는 일은 OpenAPI→MCP 전환 계층의 몫이고, FastMCP가 route map으로 이미 제공한다. `from_openapi_spec`은 추가 키워드 인자를 `FastMCP.from_openapi`로 그대로 전달하므로 route map이 그대로 통과되고, 제외된 operation은 호출 자체가 들어오지 않으므로 SOAP 브리지도 그대로 동작한다.

```python
from fastmcp.server.providers.openapi import MCPType, RouteMap

mcp = spec2openapi.from_openapi_spec(
    spec,
    route_maps=[
        RouteMap(tags={"OrderService"}, mcp_type=MCPType.TOOL),  # 이것만 유지
        RouteMap(mcp_type=MCPType.EXCLUDE),                      # 나머지 제외
    ],
)
```

WSDL에서 변환된 스펙은 모든 operation에 서비스 이름이 태그로 붙어 있으므로, 서비스 단위 서브셋에는 별도 태깅이 필요 없다. 참조 CLI(`spec2openapi serve`)는 route map을 노출하지 않으므로, 서브셋이 필요하면 파이썬 진입점을 쓴다.

## x-soap 확장 명세 (런타임 구현 계약)

오퍼레이션 레벨 `paths.*.post.x-soap`:

| 필드 | 의미 |
|---|---|
| `operation` / `service` / `port` | WSDL상의 이름 |
| `soapAction` | SOAPAction 헤더 값 |
| `soapVersion` | `"1.1"` 또는 `"1.2"` |
| `style` | `document` 또는 `rpc` |
| `endpoint` | soap:address (런타임에서 오버라이드 가능) |
| `input.element` / `input.namespace` | 요청 wrapper 엘리먼트 QName |
| `output.element` / `output.namespace` | 응답 wrapper 엘리먼트 QName (one-way면 없음) |
| `headers[]` | soap:header 파트: `{part, element, namespace, schema}` |
| `faults[]` | 선언된 fault: `{name, element, namespace, schema}` |

`headers[]`로 선언된 soap:header 값은 도구 인자로 전달되지 않는다 — 런타임이 `BridgeOptions.soap_headers` / `SPEC2OPENAPI_SOAP_HEADERS`(part 또는 element 이름을 키로 하는 JSON 객체)로 공급한다. 값이 설정되지 않은 헤더는 경고를 로그로 남기고(오퍼레이션·헤더별 최초 1회) 생략한 채 전송된다.

XML 직렬화 규칙(스키마의 `xml` 어노테이션):

- `xml.name` / `xml.namespace`: 엘리먼트 로컬명과 네임스페이스. namespace가 없으면 unqualified로 직렬화한다(rpc 파트, elementFormDefault=unqualified).
- `xml.attribute: true`: XML attribute로 직렬화.
- `xml.x-text: true`: 자식 엘리먼트가 아니라 부모의 텍스트 내용(simpleContent의 값).
- 배열 프로퍼티: 같은 이름의 엘리먼트 반복.
- `properties`의 키 순서 = XSD sequence 순서. 스펙 후처리 시 순서를 바꾸면 안 된다.
- `x-soap-choice`: 그룹당 하나만 넣어야 하는 프로퍼티 목록(스키마 차원에서는 전부 optional 처리됨). `members`의 각 항목은 `xsd:choice`의 **브랜치** 하나다. 보통은 프로퍼티 이름 하나지만, 브랜치가 `sequence`인 경우에는 이름의 **리스트**가 온다 — 그 원소들은 함께 묶여 한 단위로 선택되거나 생략되며, 서로 배타적인 관계가 아니다.
- `x-soap-substitution`: substitution group 값의 표식. JSON은 멤버 이름을 키로 한 자기서술 단일키 객체(`{"creditCard": {…}}`)이며, 와이어에는 그 멤버 element 자체가 실린다 — head element는 절대 등장하지 않는다.

루트 `x-soap`에는 원본 WSDL 경로, 생성기 버전, 스킵된 오퍼레이션 목록이 기록된다.

## SOAP 브리지 ([mcp] extra) — SOAP 스펙 서빙에 필수

`from_openapi_spec()`와 SOAP 브리지(커스텀 httpx transport)가 위 계약을 구현한다. tool 호출 JSON을 SOAP envelope으로 직렬화하고, 응답 XML을 응답 스키마에 맞춰 타입이 지정된 JSON으로 되돌리며, SOAP Fault는 MCP tool 에러로 매핑한다. **SOAP 변환 스펙은 이 계약을 구현하지 않고는 서빙할 수 없다.** 표준 OpenAPI 런타임만으로는 불가능하다. 반대로 Swagger 변환(순수 REST) 스펙은 `[mcp]` 없이 어떤 OpenAPI 런타임으로도 서빙된다.

> **SOAP + REST 혼합 스펙 주의.** 현재 참조 런타임은 한 경로라도 `x-soap`이 있으면 *전체* 트래픽을 SOAP 브리지로 라우팅하므로, 혼합 스펙의 REST 오퍼레이션은 올바르게 서빙되지 않는다. 해결 전까지 SOAP 스펙과 REST 스펙을 분리해서 쓸 것.

런타임 환경변수: `SPEC2OPENAPI_ENDPOINT`(엔드포인트 오버라이드), `SPEC2OPENAPI_AUTH`(`basic`|`wsse`), `SPEC2OPENAPI_USERNAME`/`SPEC2OPENAPI_PASSWORD`, `SPEC2OPENAPI_TIMEOUT`, `SPEC2OPENAPI_VERIFY`, `SPEC2OPENAPI_TRUST_ENV`, `SPEC2OPENAPI_SOAP_HEADERS`(선언된 `soap:header` 파트 값; part 이름 또는 element 이름을 키로 하는 JSON 객체).

`Dockerfile`(고정 이미지)과 `k8s/example.yaml`(ConfigMap으로 스펙을 교체하고 Secret으로 자격증명 주입)이 쿠버네티스 운영 예시다. 자체 런타임을 만든다면 `src/spec2openapi/bridge.py`를 참조 구현으로 삼으면 된다.

## 테스트

```bash
python -m pytest tests/
```

테스트 스위트는 다음으로 구성된다. 변환 단위 테스트, Swagger 업그레이드 테스트, envelope 직렬화/역직렬화 테스트, 목 SOAP 서버를 상대로 한 e2e 테스트(MCP tool 호출→SOAP 왕복이며 rpc·simpleContent·choice·재귀 타입·unqualified form을 포함), 전체 픽스처에 대한 OpenAPI 3.0/3.1 FastMCP 라운드트립, 그리고 실전 난제 패턴을 다룬 스트레스 테스트(재귀·순환 $ref, 4단 중첩, 대형 enum, 네임스페이스 간 타입명 충돌, 서비스 간 동명 오퍼레이션, 특수문자 경로, 깊은 allOf 체인). 생성된 스펙 샘플은 `examples/`에 있다.

추가로 opt-in **코퍼스 스윕**이 있다: 공개 [APIs.guru](https://apis.guru) 디렉터리의 실전 Swagger 2.0 문서를 층화 표본으로 받아(테스트 시 다운로드·로컬 캐시, 저장소에는 커밋하지 않음) 모든 출력에 openapi-spec-validator(3.0/3.1)와 실제 `FastMCP.from_openapi()` 라운드트립을 적용한다. `python -m pytest -m corpus`로 실행한다(네트워크 필요). 알려진 실패는 `tests/corpus/known_failures.txt`에 이슈 링크와 함께 관리해 회귀만 실패로 잡는데, **이 목록은 현재 비어 있다**: 0.3.0 사이클에서 APIs.guru의 Swagger 2.0 전량(테스트 가능한 975개)을 돌려 발견된 변환기 결함을 전부 수정했고, 표본의 모든 문서가 세 검사를 통과한다.

참고: FastMCP는 tool 이름을 `[A-Za-z0-9_]`로 정규화하므로, 이 라이브러리는 operationId를 처음부터 그 알파벳으로 생성/정규화해 "tool 이름 == operationId"가 항상 성립하게 한다(정규화 발생 시 `x-s2o.assumptions`에 기록, `validate`가 리네임을 감지해 안내).

## 프로젝트 구조

```
src/spec2openapi/
  parser.py    zeep 기반 WSDL 파싱 + 원본 XSD 스크래핑(파셋/문서화)
  schema.py    XSD -> JSON Schema (xml 어노테이션, choice, simpleContent, 파셋)
  openapi.py   OpenAPI 3.0/3.1 + x-soap 조립
  swagger.py   Swagger 2.0 -> OpenAPI 3.x 업그레이드 (x-s2o 리포트)
  convert.py   코어 공개 API (convert_wsdl / load_spec / dump_spec)
  checks.py    구조화 검증 (verify / VerifyReport)
  cli.py       convert / upgrade / inspect / validate / serve
  bridge.py    [mcp] SOAP 브리지: JSON <-> SOAP envelope httpx transport
  server.py    [mcp] FastMCP 결합 (from_openapi_spec / from_wsdl)
Dockerfile     참조 런타임 이미지
k8s/           ConfigMap + Deployment + Service 예시
```
