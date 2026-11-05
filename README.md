# Server Status Monitor

여러 대의 리눅스 서버에 **에이전트 설치 없이 SSH로 접속**해 시스템 로그를 주기적으로 수집하고, 규칙 기반으로 **하드웨어 장애와 서버 오류**를 탐지하는 중앙 모니터링 서비스입니다.

- 디스크 I/O 오류, SMART 경고, RAID degraded, ECC 메모리 오류, CPU MCE/과열
- OOM Kill, 커널 패닉, 세그폴트, 파일시스템 오류, systemd 서비스 실패, 네트워크 링크 다운

## 구성

```
┌──────────┐   SSH (읽기 전용 monitor 계정)   ┌───────────────┐
│ collector│ ───────────────────────────────▶ │ 대상 서버 N대  │
└────┬─────┘                                  └───────────────┘
     │ 규칙 분석 → 이벤트 upsert
     ▼
┌──────────┐        ┌──────────┐
│ Postgres │ ◀───── │   api    │ ◀── REST (FastAPI)
└──────────┘        └──────────┘
```

| 서비스 | 역할 |
|---|---|
| `db` | PostgreSQL 16. 서버 목록, 수집 상태, 이벤트 저장 |
| `api` | FastAPI REST API와 관리자 대시보드 (`:8000`). 서버 등록·관리, 이벤트 조회·확인 처리 |
| `collector` | 수집 워커. SSH로 로그를 가져와 규칙 엔진으로 분석 |
| `target1`, `target2` | 로컬 테스트용 SSH 서버 (`test` 프로필에서만 기동) |

## 동작 방식

1. **기능 탐지**: 서버를 처음 수집할 때 사용 가능한 로그 소스를 자동으로 확인합니다.
2. **로그 수집**: 탐지된 소스별로 60초마다 새 로그만 증분 수집합니다. 커서/오프셋은 `CollectorState`에 저장됩니다.
   | 소스 | 내용 |
   |---|---|
   | `journal` | `journalctl` (journald가 있으면 syslog 파일 수집은 생략해 중복 방지) |
   | `dmesg` | 커널 링 버퍼 (err 이상) |
   | `systemd` | 실패한 systemd 유닛 |
   | `logfile` | `/var/log/syslog`, `/var/log/messages` (로테이션 감지 포함) |
3. **규칙 분석**: `backend/rules/*.yaml`의 정규식 규칙과 로그 줄을 매칭합니다. 매칭되면 카테고리(`hardware` / `server_error`)와 심각도(`critical` / `warning`)가 붙은 이벤트가 됩니다.
4. **중복 묶음**: 같은 서버에서 같은 내용(fingerprint)이 반복되면 새 이벤트를 만들지 않고 `count`와 `last_seen`만 갱신합니다. 이미 확인 처리한 이벤트라도 다시 발생하면 미확인으로 돌아갑니다.
5. **서버 상태 계산**
   - `unreachable`: 연속 수집 실패가 3회 이상
   - `critical` / `warning`: 최근 24시간 안에 확인하지 않은 해당 심각도 이벤트가 있음
   - `ok`: 그 외

collector는 15초마다 돌며 동시 접속 수를 세마포어로 제한합니다(기본 50). 응답이 느린 서버는 다른 서버 수집을 막지 않습니다.

## 보안

- SSH 개인키/비밀번호는 **Fernet으로 암호화**해서 DB에 저장합니다. API 응답에는 포함되지 않습니다.
- 처음 접속할 때 호스트 키를 고정(pin)하고, 이후 키가 바뀌면 **자동으로 신뢰하지 않고** 수집을 중단합니다. 확인한 뒤 `reset-host-key` API로 해제하세요.
- 대상 서버에는 비밀번호 로그인이 막힌 전용 `monitor` 계정을 씁니다. 로그 그룹(`adm`, `systemd-journal`) 권한과 최소한의 sudo 화이트리스트만 부여합니다.

## 빠른 시작 (로컬 테스트)

필요한 것: Docker, Docker Compose

```bash
# 1) 환경 변수 파일 만들기
cp .env.example .env
# FERNET_KEY 생성 후 .env에 넣기 (둘 중 하나)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
openssl rand 32 | base64 | tr '+/' '-_'

# 2) 테스트 대상 서버용 SSH 키 만들기
ssh-keygen -t ed25519 -N "" -f deploy/test-targets/keys/id_ed25519

# 3) 전체 기동 (테스트 대상 서버 포함)
docker compose --profile test up -d --build

# 4) 상태 확인
curl localhost:8000/healthz
```

- 관리자 대시보드: http://localhost:8000/admin/ (`/`로 접속해도 이동)
- API 문서(Swagger): http://localhost:8000/docs

## 관리자 대시보드

`backend/app/static/admin/index.html` 한 파일로 된 정적 페이지입니다. api 서버가 직접 서빙하며 빌드 과정이 없습니다.

- **요약 카드**: 전체 서버 수, 상태별 서버 수, 미확인 이벤트 수(심각도·분류별)
- **서버 관리**: 추가, 수정, 삭제, 활성화/비활성화, 연결 테스트, 호스트 키 초기화
  - 서버를 추가하면 바로 연결 테스트를 실행합니다.
  - 수정할 때 인증정보 칸을 비워 두면 기존 값을 유지합니다.
- **수집 상세**: 소스별 마지막 실행·성공 시각, 결과, 수집 건수, 규칙 일치 건수, 소요 시간, 실행 명령과 오류
  - 접속은 되지만 로그 수집 명령이 실패하면 서버 상태가 `수집 오류`(`degraded`)로 표시됩니다. `critical`/`warning` 이벤트가 있으면 그 상태가 먼저 표시되며, 수집 오류 내용은 오류 칸에서 볼 수 있습니다.
- **이벤트**: 서버·심각도·분류·확인 여부로 필터, 제목/메시지 검색, 확인 처리
  - 서버 이름을 누르면 그 서버의 이벤트만 봅니다.
  - 메시지 칸에는 로그의 의미(규칙별 고정 설명)와 원본 로그가 함께 표시됩니다.
  - 메시지를 누르면 상세 창에서 이벤트 정보와 발생 이력을 보고 원본 로그를 복사할 수 있습니다. 이력은 날짜로 검색할 수 있습니다.
- **발생 이력**: 모든 이벤트의 원본 로그를 최신순으로 보여주며, 서버, 심각도, 이벤트, 날짜(브라우저 시간대 기준)로 검색할 수 있습니다. 로그를 누르면 해당 이벤트의 상세 창이 열립니다.
  - `systemd` 소스는 실패 상태를 수집할 때마다 다시 보고하므로, 이벤트가 새로 생기거나 다시 열릴 때만 이력에 저장합니다.
- 10초마다 자동으로 새로고침합니다(체크박스로 끌 수 있음).
- 다크 모드를 지원합니다.

> ⚠️ 대시보드와 API에는 **인증이 없습니다**. 신뢰할 수 있는 내부망에서만 쓰고, 외부에 열어야 한다면 리버스 프록시에 인증을 붙이세요.

### 테스트 서버 등록 및 이벤트 확인

```bash
# 서버 등록 (secret에는 개인키 원문을 JSON 문자열로 넣어야 함)
docker compose exec -T api python -c "
import json,sys,urllib.request
body=json.dumps({'name':'target1','host':'target1','secret':sys.stdin.read(),'tags':['test']}).encode()
print(urllib.request.urlopen(urllib.request.Request('http://localhost:8000/api/servers',body,{'Content-Type':'application/json'})).read().decode())
" < deploy/test-targets/keys/id_ed25519

# 연결 테스트
curl -X POST localhost:8000/api/servers/1/test

# 장애 로그 흉내내기
docker compose exec target1 sh -c 'echo "$(date "+%b %d %H:%M:%S") target1 kernel: Out of memory: Killed process 4242 (java)" >> /var/log/syslog'

# 최대 1분 뒤 이벤트 확인
curl localhost:8000/api/events
curl localhost:8000/api/summary
```

### 종료

```bash
docker compose --profile test down      # 데이터 유지
docker compose --profile test down -v   # DB 데이터까지 삭제
```

## 실제 서버 등록

1. 중앙 서버에서 수집용 키를 만듭니다: `ssh-keygen -t ed25519 -f monitor_key`
2. 모니터링할 각 서버에서 root로 계정 설정 스크립트를 실행합니다:
   ```bash
   sudo sh deploy/monitor-user-setup.sh "$(cat monitor_key.pub)"
   ```
   `monitor` 계정을 만들고(비밀번호 로그인 차단), 로그 그룹에 추가한 뒤, 서버에 실제로 있는 명령(`dmesg`, `journalctl`, `smartctl`, `ipmitool`, `ras-mc-ctl`, `mdadm`)만 sudo 화이트리스트에 넣습니다.
3. `POST /api/servers`로 서버를 등록하고 `POST /api/servers/{id}/test`로 연결을 확인합니다.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/servers` | 서버 목록 |
| POST | `/api/servers` | 서버 등록 |
| GET / PATCH / DELETE | `/api/servers/{id}` | 서버 조회 / 수정 / 삭제 |
| GET | `/api/servers/{id}/sources` | 소스별 마지막 수집 결과 |
| POST | `/api/servers/{id}/test` | SSH 연결 테스트 및 기능 탐지 |
| POST | `/api/servers/{id}/reset-host-key` | 고정된 호스트 키 해제 |
| GET | `/api/events` | 이벤트 조회 (`server_id`, `severity`, `category`, `acknowledged`, `since`, `q`, `limit`, `offset` 필터) |
| GET | `/api/events/{id}` | 이벤트 하나 조회 |
| GET | `/api/occurrences` | 발생 이력 조회, 최신순 (`server_id`, `event_id`, `severity`, `from`(포함), `to`(미포함), `limit`, `offset`). 조건에 맞는 건수 `total`을 함께 반환. `offset`에서 10,000건을 넘으면 세기를 멈추고 `total_capped`가 `true` |
| POST | `/api/events/{id}/ack` | 이벤트 확인 처리 |
| GET | `/api/summary` | 서버 상태별 개수, 미확인 이벤트 심각도·카테고리별 개수 |
| GET | `/healthz` | 헬스 체크 |

서버 등록 예시 본문:

```json
{
  "name": "web-01",
  "host": "10.0.0.11",
  "port": 22,
  "username": "monitor",
  "auth_type": "key",
  "secret": "-----BEGIN OPENSSH PRIVATE KEY-----\n...",
  "tags": ["web", "prod"]
}
```

## 탐지 규칙

`backend/rules/default.yaml`에 정의되어 있습니다. 같은 디렉터리에 YAML 파일을 추가하면 규칙을 확장할 수 있습니다. 규칙의 `description`은 대시보드에 로그의 의미로 표시되며, 바꾸면 API를 다시 시작해야 반영됩니다.

| 규칙 ID | 카테고리 | 심각도 |
|---|---|---|
| `disk.io_error`, `disk.ata_error`, `disk.smart` | hardware | critical |
| `raid.degraded` | hardware | critical |
| `mem.ecc_uncorrectable` | hardware | critical |
| `mem.ecc_correctable` | hardware | warning |
| `cpu.mce` | hardware | critical |
| `cpu.thermal` | hardware | warning |
| `net.link_down` | hardware | warning |
| `fs.error` | server_error | critical |
| `mem.oom` | server_error | critical |
| `kernel.panic` | server_error | critical |
| `proc.segfault` | server_error | warning |
| `service.failed`, `service.failed_log` | server_error | warning |
| `proc.privilege_drop` | server_error | warning |
| `auth.sudo_denied` | server_error | critical |

## 설정 (`.env`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `FERNET_KEY` | (필수) | SSH 인증정보 암호화 키. **잃어버리면 저장된 인증정보를 복호화할 수 없습니다.** |
| `COLLECT_TICK_SECONDS` | `15` | collector 루프 주기(초) |
| `MAX_CONCURRENCY` | `50` | 동시 SSH 수집 수 |
| `SSH_CONNECT_TIMEOUT` | `15` | SSH 접속 타임아웃(초) |
| `SSH_COMMAND_TIMEOUT` | `30` | 원격 명령 타임아웃(초) |
| `UNREACHABLE_AFTER_FAILURES` | `3` | 몇 번 연속 실패하면 `unreachable`로 볼지 |
| `STATUS_WINDOW_HOURS` | `24` | 서버 상태 계산에 반영할 이벤트 기간(시간) |
| `WORKER_INDEX` / `WORKER_COUNT` | `0` / `1` | collector 샤딩 설정 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | (없음) | 텔레그램 알림 봇 토큰과 채팅 ID. 둘 다 있어야 알림이 켜집니다. |
| `TELEGRAM_MIN_SEVERITY` | `critical` | 알림을 보낼 최소 심각도 (`critical`, `warning`, `info`) |

### 텔레그램 알림

알림은 다음 경우에 보냅니다.

- 설정한 심각도 이상의 이벤트가 새로 발생했을 때
- 확인 처리한 이벤트가 다시 발생했을 때 (확인 전의 반복 발생은 보내지 않습니다)
- 서버가 `unreachable`이 되었을 때와 거기서 복구되었을 때

보낼 알림은 `notifications` 테이블에 먼저 저장되므로, 텔레그램 장애나 collector 재시작 후에도 이어서 전송됩니다. collector가 꺼져 있는 동안에는 알림도 오지 않으며, 꺼졌다는 사실도 알려주지 않습니다.

### 수평 확장

서버가 많아지면 collector를 여러 개 띄웁니다. 각 워커에 `WORKER_COUNT=N`, `WORKER_INDEX=0..N-1`을 주면, 워커마다 `server_id % N == WORKER_INDEX`인 서버만 담당합니다.

## 개발

```bash
cd backend
pip install -r requirements-dev.txt
pytest
```

`tests/test_notifier_db.py`는 실제 PostgreSQL이 필요해서 `TEST_DATABASE_URL`이 있을 때만 실행됩니다. **이 테스트는 테이블을 모두 삭제하므로** 이름에 `test`가 들어간 일회용 DB를 지정하세요.

## 알려진 제약

- 대시보드와 API에 인증이 없습니다.
- DB 스키마는 기동할 때 `create_all`로 만듭니다. 아직 Alembic 마이그레이션은 없습니다.
- 발생 이력(`event_occurrences`)은 자동으로 삭제되지 않습니다. 이벤트가 반복되는 만큼 DB가 계속 커지므로 용량을 지켜봐야 합니다.
- 로그 소스 수집 주기가 60초라서 장애 로그가 이벤트로 보이기까지 최대 약 1분 걸립니다.
- 테스트 대상 컨테이너(`target1/2`)는 같은 이미지에서 만들어져 호스트 키를 공유하고, 컨테이너 안에서는 `dmesg`가 동작하지 않습니다. 그래서 로그 파일 경로만 검증할 수 있습니다.
