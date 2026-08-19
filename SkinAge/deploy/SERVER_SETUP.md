# 가비아 서버 초기 설정 가이드

`.omc/plans/gabia-deploy-plan.md`의 설계를 실제 가비아 서버(Ubuntu, 공인 IP `1.201.116.7`, DuckDNS `remine-demo.duckdns.org`)에 배포하면서 검증된 최종 절차입니다. 순서대로 진행하세요. 이 문서 전체에서:

- `<GHCR_OWNER>` = **GitHub 계정/조직명**입니다 (도메인이 아닙니다!). 이 레포는 `arror1784`.
- `<domain>` = DuckDNS 서브도메인 전체 (예: `remine-demo.duckdns.org`)

## 0. 서버 스펙 실측

```bash
ssh <server> "free -h && nproc && df -h"
docker pull hello-world   # (Docker 설치 후) 레지스트리 대역폭 사전 확인
```

- **RAM < 4GB**: 아래 절차 그대로(GHCR 이미지 pull 방식) 진행하고, "9. 스왑 설정"을 반드시 수행합니다.
- **RAM ≥ 4GB**: `docker-compose.prod.yml`을 서버에서 직접 빌드하는 방식으로 단순화할 수 있습니다(이 문서는 GHCR 방식 기준 — 직접 빌드로 전환 시 `.github/workflows/deploy.yml`의 build-and-push job을 생략하고 deploy job에서 `docker compose build`를 실행하도록 바꾸세요).

## 1. Docker 설치

클린 Ubuntu에는 `docker-compose-plugin` 패키지가 기본 저장소에 없습니다. 공식 설치 스크립트를 사용하세요.

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

**그룹 반영은 지금 로그인해 있는 세션엔 바로 적용되지 않습니다.** 아래 둘 중 하나로 반영하세요.

```bash
newgrp docker      # 같은 세션에서 바로 적용 (가장 빠름)
# 또는: exit 후 다시 SSH 접속
```

확인: `groups`에 `docker`가 포함돼 있어야 합니다.

## 2. certbot 설치

```bash
sudo apt-get update && sudo apt-get install -y certbot
```

## 3. 방화벽

두 군데를 다 열어야 합니다 — **가비아 콘솔의 인바운드 규칙(보안그룹)**과 **서버 안의 ufw**는 별개입니다.

**가비아 콘솔**: 인스턴스 방화벽/보안그룹 메뉴에서 인바운드 규칙 추가
- TCP 80, 소스 `0.0.0.0/0` (프리셋에 "HTTP"가 있으면 그걸로)
- TCP 443, 소스 `0.0.0.0/0` (프리셋에 "HTTPS"가 있으면 그걸로)
- (22는 보통 이미 열려 있음 — 지금 SSH로 접속되고 있다면 확인 불필요)

**서버 안 ufw**:
```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

> **경고**: Docker는 published port를 iptables `DOCKER-USER` 체인에 직접 기록하여 **ufw 규칙을 우회합니다.** `docker-compose.prod.yml`의 `postgres`/`redis` 서비스에는 절대 `ports:`를 추가하지 마세요 — 이번 배포는 `demo-login` 무인증 JWT 발급 엔드포인트를 그대로 두므로, DB가 인터넷에 노출되면 사실상 전체가 노출됩니다.

> 가비아 콘솔의 인바운드 규칙을 빠뜨리면 `certbot certonly`가 `Timeout during connect (likely firewall problem)`로 실패합니다 — 실제로 겪었던 증상이니, 인증서 발급 전에 반드시 위 두 규칙을 먼저 확인하세요.

## 4. DuckDNS

1. https://www.duckdns.org 에서 계정 생성, 서브도메인 발급 (예: `remine-demo`)
2. A 레코드를 가비아 공인 IP로 설정
3. 이 문서 전체에서 `<domain>`은 그 서브도메인 전체(`remine-demo.duckdns.org`)로 치환

## 5. 레포 클론

`arror1784/Remine` 레포는 **public**이므로 HTTPS 클론이면 충분합니다 — 별도 SSH 키/PAT 등록 없이 `git clone`/`git fetch`가 인증 없이 동작합니다.

```bash
sudo mkdir -p /opt/remine && sudo chown $USER:$USER /opt/remine
git clone https://github.com/arror1784/Remine.git /opt/remine
cd /opt/remine
```

(레포가 나중에 private으로 바뀌면 그때는 read-only deploy key(GitHub repo → Settings → Deploy keys → Add)를 등록하고 `git@github.com:...` SSH 클론으로 전환하세요.)

## 6. 시크릿 배치

```bash
# /opt 자체는 root 소유라(Step 5에서 chown한 건 /opt/remine 디렉터리뿐), 그 바로
# 밑에 파일을 만들려면 sudo가 필요합니다.
sudo cp .env.example /opt/remine.env
sudo chown $USER:$USER /opt/remine.env
chmod 600 /opt/remine.env
```

에디터로 `/opt/remine.env`를 열어 값을 채웁니다.

```bash
JWT_SECRET=$(openssl rand -hex 32)          # 로 생성한 값을 붙여넣기
DB_URL=jdbc:postgresql://postgres:5432/remine
DB_USER=remine_user
DB_PASSWORD=<openssl rand -hex 20 로 생성>
REDIS_HOST=redis
POSTGRES_DB=remine
POSTGRES_USER=remine_user
POSTGRES_PASSWORD=<DB_PASSWORD와 동일한 값>
OPENAI_API_KEY=<새로 발급받은 키 — application-local.yml의 기존 키 재사용 금지>
STORAGE_UPLOAD_DIR=/app/uploads
STORAGE_PUBLIC_BASE_URL=https://<domain>
CORS_ALLOWED_ORIGINS=https://<domain>
TZ=Asia/Seoul
IMAGE_TAG=latest
GHCR_OWNER=arror1784                         # GitHub 계정명 — 도메인 아님!
```

- `DB_USER`/`POSTGRES_USER`, `DB_PASSWORD`/`POSTGRES_PASSWORD`는 **각각 같은 값**이어야 합니다(하나는 Postgres 컨테이너 초기화용, 하나는 백엔드 접속용).
- `JWT_SECRET`, `DB_PASSWORD`는 `openssl rand -hex 32` / `openssl rand -hex 20`로 생성.

**`.env`는 레포 클론 디렉터리 밖(`/opt/remine.env`)에 둡니다** — `docker-compose.prod.yml`은 `app-api.env_file`을 `/opt/remine.env`(절대경로)로 참조합니다. 매 배포마다 `/opt/remine`이 `git reset --hard`로 초기화되므로, 클론 디렉터리 안에 두면 지워집니다.

**GitHub Secrets에도 최소 이 4개는 백업**해 두세요(Settings → Secrets and variables → Actions → New repository secret) — 서버가 사라지면 `/opt/remine.env`가 유일한 원본이 되어버립니다.

| Name | Value |
|---|---|
| `JWT_SECRET` | `/opt/remine.env`와 동일한 값 |
| `DB_PASSWORD` | 동일 |
| `POSTGRES_PASSWORD` | 동일(`DB_PASSWORD`와 같은 값) |
| `OPENAI_API_KEY` | 동일 |

## 7. GHCR 접근

소스 레포가 public이므로 코드는 이미 누구나 볼 수 있고, 이미지에도 시크릿이 없습니다("prod" 프로필은 전부 런타임 env로 주입됨) — GHCR 패키지를 **public으로 전환**하면 서버에서 별도 로그인 없이 `docker compose pull`이 그대로 동작합니다.

## 8. 첫 배포 순서

GHCR 패키지는 최초 push 시 기본 **private**으로 생성됩니다(레포가 public이어도 패키지는 별도 설정). 첫 워크플로 실행 시 build job 직후 deploy job이 곧바로 pull을 시도하면 401로 실패하므로:

1. `main`에 최초 push → build-and-push job만 성공 확인 (`https://github.com/arror1784/Remine/actions`)
2. GitHub 웹 UI → `https://github.com/arror1784?tab=packages` (또는 레포 페이지 우측 사이드바 Packages) → `remine-backend`/`remine-frontend` 각 패키지 → Package settings → 아래로 스크롤 Danger Zone → Change visibility → **Public**
3. 이후 push부터는 서버에서 로그인 없이 pull이 되므로 전체 워크플로우가 문제없이 통과합니다.

## 9. 스왑 설정 (RAM < 4GB인 경우 필수)

```bash
sudo swapon --show   # 이미 떠 있으면 아래는 건너뛰기
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

(재실행하려면 `sudo swapoff /swapfile && sudo rm -f /swapfile`로 먼저 지우고 처음부터 — 이미 `swapon`된 파일에 `fallocate`를 다시 걸면 `Text file busy`로 실패합니다.)

## 10. 최초 인증서 발급 (2단계 부트스트랩)

```bash
cd /opt/remine
mkdir -p certbot-webroot

# 1단계: HTTP 전용 conf로 frontend만 기동 (app-api 없이도 안전)
cp deploy/nginx/nginx.http-only.conf deploy/nginx/active.conf
GHCR_OWNER=arror1784 docker compose --env-file /opt/remine.env -f docker-compose.prod.yml up -d frontend

# 인증서 발급 (반드시 3단계 방화벽부터 열어둔 상태여야 함)
sudo certbot certonly --webroot -w /opt/remine/certbot-webroot -d remine-demo.duckdns.org

# 2단계: HTTPS conf로 교체
cp deploy/nginx/nginx.https.conf deploy/nginx/active.conf
sed -i "s/<domain>/remine-demo.duckdns.org/g" deploy/nginx/active.conf

# 전체 스택 기동
GHCR_OWNER=arror1784 docker compose --env-file /opt/remine.env -f docker-compose.prod.yml up -d

# frontend는 1단계에서부터 이미 떠 있던 컨테이너이므로 restart로 새 conf를 반영해야 함
docker compose --env-file /opt/remine.env -f docker-compose.prod.yml restart frontend
```

> **주의**: `active.conf`는 항상 `cp`로 생성하세요. 심볼릭 링크로 만들면 Docker가 컨테이너 기동 시점에 링크 대상을 해석해 바인드하므로, 이후 링크만 바꿔도 `nginx -s reload`가 옛 설정을 계속 읽습니다.
>
> **`sed -i`도 같은 문제를 일으킵니다** (실제로 겪은 문제). GNU `sed -i`는 파일을 제자리에서 고치지 않고 임시 파일을 만들어 원본에 rename으로 덮어씁니다 — 이 과정에서 inode가 바뀝니다. `active.conf`를 이미 bind mount로 물고 있는 컨테이너가 떠 있는 상태에서 `sed -i`로 그 파일을 고치면, 컨테이너는 바뀐 내용을 못 보고 계속 옛 inode(옛 내용)를 봅니다. 그래서 위 절차에서 `sed -i` 다음에 `nginx -s reload`가 아니라 **`docker compose restart frontend`**를 씁니다 — restart는 컨테이너를 내렸다 다시 띄우면서 bind mount를 처음부터 다시 맺으므로 확실합니다.

## 11. 인증서 자동 갱신

apt가 설치한 `certbot.timer`(기본 활성화) 하나만 사용하고 별도 cron은 추가하지 마세요(이중 실행 시 Let's Encrypt 레이트 리밋 소진 위험).

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/00-reload-nginx.sh > /dev/null <<'EOF'
#!/bin/sh
docker compose --env-file /opt/remine.env -f /opt/remine/docker-compose.prod.yml exec -T frontend nginx -s reload
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/00-reload-nginx.sh

# 검증 (--run-deploy-hooks 없이는 훅 자체가 검증되지 않음)
sudo certbot renew --dry-run --run-deploy-hooks
```

(훅 스크립트에 `--env-file /opt/remine.env`를 빠뜨리면 `POSTGRES_*`/`GHCR_OWNER` 관련 경고와 함께 실패합니다 — 위 예시에는 이미 포함되어 있습니다.)

## 12. GitHub Actions CI/CD 연결 (자동 배포)

여기까지는 전부 수동으로 서버에서 실행한 것이고, 이제 `main`에 push할 때마다 자동으로 이 서버까지 배포되도록 연결합니다.

**위치**: `https://github.com/arror1784/Remine/settings/secrets/actions` → **New repository secret**

| Secret 이름 | 값 | 비고 |
|---|---|---|
| `SSH_HOST` | 가비아 공인 IP | 예: `1.201.116.7` |
| `SSH_USER` | `ubuntu` | 지금 SSH 접속할 때 쓰는 계정 |
| `SSH_PRIVATE_KEY` | SSH 접속용 개인키 **전체 내용** | `cat ~/Downloads/your-key.pem` 결과를 `-----BEGIN...`부터 `-----END...`까지 통째로 |
| `SSH_HOST_FINGERPRINT` | (선택) 서버 호스트 키 지문 | 아래 참고 — **비워둬도 동작합니다** |

`SSH_HOST_FINGERPRINT`를 채우려면 로컬에서:
```bash
ssh-keygen -lf ~/.ssh/known_hosts -F <가비아 공인 IP>
```
나오는 `SHA256:...` 값을 그대로 사용. **`ssh-keyscan -t rsa ... | ssh-keygen -lf -`로 뽑으면 실제 협상되는 키 종류와 달라 `host key fingerprint mismatch`로 실패할 수 있습니다** (실제로 겪은 문제) — `known_hosts`에서 뽑는 방법이 가장 확실합니다. 급하면 이 Secret을 아예 비워두세요 — `appleboy/ssh-action`은 fingerprint가 없으면 호스트 키 검증을 건너뛰고 접속합니다.

등록 후 확인:
```bash
gh run list --repo arror1784/Remine --limit 1
gh run rerun <run-id> --repo arror1784/Remine --failed   # 실패했던 run 재시도
```
또는 GitHub Actions 탭에서 `Build and deploy` 워크플로우 → `Run workflow` 버튼(수동 트리거, `workflow_dispatch`로 추가해둠)으로 바로 새로 실행 가능합니다.

### SSH 키를 로테이션한 경우

키를 새로 발급했다면 3곳을 모두 맞춰야 합니다:

1. **서버의 `authorized_keys`**에 새 공개키 등록 — 옛 키로 접속해서:
   ```bash
   ssh-keygen -y -f ~/Downloads/새키.pem   # 공개키 한 줄 출력
   # 그 출력을 서버 안에서:
   echo "ssh-rsa AAAA..." >> ~/.ssh/authorized_keys
   ```
   옛 키가 이미 없다면 가비아 콘솔의 웹 콘솔/VNC로 직접 로그인해서 등록.
2. **로컬**에서 새 키로 접속 확인: `ssh -i 새키.pem ubuntu@<IP>`
3. **GitHub Secrets의 `SSH_PRIVATE_KEY`**도 새 개인키 내용으로 교체

세 곳 다 맞아야 CI가 정상 배포됩니다.

## 13. 검증

```bash
docker compose --env-file /opt/remine.env -f docker-compose.prod.yml ps        # 전 서비스 healthy
docker compose --env-file /opt/remine.env -f docker-compose.prod.yml exec app-api env | grep SPRING_PROFILES_ACTIVE   # prod
curl -fsSL https://<domain>/actuator/health                                     # {"status":"UP"}
```

그리고 브라우저로 `https://<domain>` 접속 → demo-login → 체크리스트 → **1MB 초과 사진 업로드 후 정상 표시** → 응원 메시지 AI 생성 → 추억 퀴즈까지 골든 패스 1회 수행.

## 14. 재부팅 후 자동 기동 확인

```bash
sudo systemctl is-enabled docker   # enabled 여야 함 (get.docker.com 스크립트가 보통 자동 설정)
docker compose --env-file /opt/remine.env -f docker-compose.prod.yml ps
```

## 15. 롤백

```bash
ssh <server> "cd /opt/remine && git checkout <이전-sha> -- docker-compose.prod.yml deploy/ && IMAGE_TAG=<이전-sha> docker compose --env-file /opt/remine.env -f docker-compose.prod.yml up -d"
```

## 트러블슈팅 (실제로 겪은 문제들)

| 증상 | 원인 | 해결 |
|---|---|---|
| `cp: cannot create regular file '/opt/remine.env': Permission denied` | `/opt`가 root 소유 | 6단계처럼 `sudo cp` + `sudo chown` |
| `permission denied while trying to connect to the docker API` | `usermod -aG docker` 후 세션 미반영 | `newgrp docker` 또는 재로그인 |
| `certbot: command not found` | 2단계 건너뜀 | `sudo apt-get install -y certbot` |
| `Timeout during connect (likely firewall problem)` (certbot) | 가비아 콘솔 인바운드 규칙에 80 없음 | 3단계 — 가비아 콘솔에서 80/443 인바운드 규칙 추가 (ufw만으론 부족) |
| `invalid repository name` (docker compose up) | `GHCR_OWNER`에 도메인을 넣음 | `GHCR_OWNER`는 GitHub 계정명(`arror1784`)이지 도메인이 아님 |
| nginx가 여전히 `<domain>` 리터럴로 인증서 로드 실패 | `sed -i`가 bind mount의 inode를 깨뜨림 | `docker compose restart frontend` |
| `curl .../actuator/health` → 503 | app-api가 `Redis ... localhost:6379` 연결 거부 (Boot 2.7에서 `spring.data.redis`는 무효 프로퍼티 — `spring.redis`가 맞음, 코드에서 수정됨) | 최신 커밋으로 재배포하면 해결됨. 여전히 발생하면 `docker compose exec app-api env \| grep REDIS_HOST`로 `redis`인지 확인 후 `up -d --force-recreate app-api`(env_file은 컨테이너 생성 시점에만 읽힘) |
| `ssh: handshake failed: ssh: host key fingerprint mismatch` (CI) | `SSH_HOST_FINGERPRINT`가 실제 협상 키와 다름 | Secret을 비우거나 `ssh-keygen -lf ~/.ssh/known_hosts -F <IP>`로 다시 뽑기 |
| CI deploy에서 `password:` 프롬프트 대기 (SSH) | 로컬에서 새 키로 접속할 때 새 공개키가 서버에 없음 | "SSH 키를 로테이션한 경우" 절차대로 `authorized_keys`에 새 공개키 등록 |
| `docker: run <run-id> cannot be rerun; This workflow run cannot be retried` | 오래된 run은 재시도 횟수 제한 | `workflow_dispatch`로 새로 트리거하거나 새 커밋 push |
