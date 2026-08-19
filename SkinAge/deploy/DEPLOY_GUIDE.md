# SkinAge API 서버 배포 가이드

이 문서는 SkinAge API를 가비아 서버(Ubuntu, 공인 IP)에 Docker로 배포하는 전체 절차를 다룹니다.
`deploy/SERVER_SETUP.md`의 Remine 프로젝트 배포 경험을 바탕으로, SkinAge 프로젝트에 맞게 재구성했습니다.

---

## 전제 조건

| 항목 | 요구사항 |
|------|----------|
| 서버 OS | Ubuntu 20.04+ |
| RAM | 최소 2GB (4GB 권장, 모델 로드 시 메모리 사용) |
| 디스크 | 최소 10GB 여유 공간 |
| 도메인 | DuckDNS 서브도메인 또는 자체 도메인 |
| 공인 IP | 가비아 서버 공인 IP |

---

## 0. 서버 스펙 확인

```bash
ssh <user>@<서버IP>
free -h && nproc && df -h
```

- RAM < 4GB 이면 Step 8의 스왑 설정을 반드시 수행하세요.

---

## 1. Docker 설치

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker   # 현재 세션에 바로 반영
```

확인:
```bash
docker --version
docker compose version
```

---

## 2. 방화벽 설정

**가비아 콘솔** (인바운드 규칙 추가):
- TCP 22 (SSH)
- TCP 80 (HTTP - certbot 인증용)
- TCP 443 (HTTPS)

**서버 ufw**:
```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

---

## 3. 도메인 설정 (DuckDNS)

1. https://www.duckdns.org 가입 및 서브도메인 생성 (예: `skinage-api`)
2. A 레코드를 서버 공인 IP로 설정
3. 이후 `<domain>` = `skinage-api.duckdns.org`로 치환

---

## 4. 프로젝트 클론

```bash
sudo mkdir -p /opt/skinage && sudo chown $USER:$USER /opt/skinage
git clone https://github.com/arror1784/SkinAge.git /opt/skinage
cd /opt/skinage/SkinAge
```

---

## 5. 모델 가중치 준비

학습된 모델 파일(`best_model.pth`)이 `outputs/models/` 에 있어야 합니다.

```bash
mkdir -p outputs/models
# 방법 1: 로컬에서 scp로 전송
# scp best_model.pth <user>@<서버IP>:/opt/skinage/SkinAge/outputs/models/

# 방법 2: 서버에서 자동 다운로드 (GitHub Release에 업로드된 경우)
python3 scripts/download_weights.py
```

---

## 6. 환경 변수 설정

```bash
sudo tee /opt/skinage.env > /dev/null <<'EOF'
SKINAGE_CONFIG_DIR=/app/config
SKINAGE_MODEL_PATH=/app/outputs/models/best_model.pth
SKINAGE_DEVICE=cpu
TZ=Asia/Seoul
EOF

sudo chown $USER:$USER /opt/skinage.env
chmod 600 /opt/skinage.env
```

> GPU가 있는 서버라면 `SKINAGE_DEVICE=cuda`로 변경하세요.

---

## 7. Certbot 설치

```bash
sudo apt-get update && sudo apt-get install -y certbot
```

---

## 8. 스왑 설정 (RAM < 4GB인 경우 필수)

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 9. docker-compose.prod.yml 작성

`/opt/skinage/SkinAge/docker-compose.prod.yml`:

```yaml
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: skinage-api
    expose:
      - "8000"
    volumes:
      - ./outputs/models:/app/outputs/models:ro
    env_file:
      - /opt/skinage.env
    networks:
      - skinage-net
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

  nginx:
    image: nginx:alpine
    container_name: skinage-nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./deploy/nginx/active.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - ./certbot-webroot:/var/www/certbot:ro
    depends_on:
      api:
        condition: service_started
    networks:
      - skinage-net
    restart: unless-stopped

networks:
  skinage-net:
    driver: bridge
```

---

## 10. Nginx 설정 업데이트

SkinAge API용으로 `deploy/nginx/nginx.https.conf`를 수정합니다:

```bash
cd /opt/skinage/SkinAge
```

`deploy/nginx/nginx.https.conf` 내용을 아래로 교체하세요:

```nginx
server {
    listen 80;
    server_name _;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /etc/letsencrypt/live/<domain>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<domain>/privkey.pem;

    client_max_body_size 12m;

    # API 프록시
    location /api/ {
        proxy_pass http://api:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # Swagger 문서
    location /docs {
        proxy_pass http://api:8000/docs;
        proxy_set_header Host $host;
    }

    location /openapi.json {
        proxy_pass http://api:8000/openapi.json;
        proxy_set_header Host $host;
    }

    # 헬스체크
    location /health {
        proxy_pass http://api:8000/api/v1/health;
    }

    location / {
        return 404 '{"detail":"Not Found"}';
        add_header Content-Type application/json;
    }
}
```

---

## 11. 인증서 발급 (Let's Encrypt)

### 11-1. HTTP 전용으로 nginx 부트스트랩

```bash
cd /opt/skinage/SkinAge
mkdir -p certbot-webroot

# HTTP 전용 설정으로 시작
cp deploy/nginx/nginx.http-only.conf deploy/nginx/active.conf
docker compose -f docker-compose.prod.yml up -d nginx
```

### 11-2. 인증서 발급

```bash
sudo certbot certonly --webroot \
  -w /opt/skinage/SkinAge/certbot-webroot \
  -d <domain>
```

> `Timeout during connect` 오류 시 → Step 2 방화벽(가비아 콘솔 + ufw) 확인

### 11-3. HTTPS 전환

```bash
# HTTPS 설정으로 교체
cp deploy/nginx/nginx.https.conf deploy/nginx/active.conf
sed -i "s/<domain>/<실제도메인>/g" deploy/nginx/active.conf

# 전체 스택 기동
docker compose -f docker-compose.prod.yml up -d

# nginx에 새 설정 반영
docker compose -f docker-compose.prod.yml restart nginx
```

---

## 12. 인증서 자동 갱신

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/00-reload-nginx.sh > /dev/null <<'EOF'
#!/bin/sh
docker compose -f /opt/skinage/SkinAge/docker-compose.prod.yml exec -T nginx nginx -s reload
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/00-reload-nginx.sh

# 갱신 테스트
sudo certbot renew --dry-run
```

---

## 13. 검증

```bash
# 서비스 상태 확인
docker compose -f docker-compose.prod.yml ps

# 헬스체크
curl -fsSL https://<domain>/api/v1/health

# 이미지 분석 테스트
curl -X POST https://<domain>/api/v1/analyze \
  -F "file=@test_face.jpg" \
  -F "age=30"
```

브라우저에서 `https://<domain>/docs` 접속 → Swagger UI 확인

---

## 14. 재부팅 후 자동 기동

```bash
# Docker 자동 시작 확인
sudo systemctl is-enabled docker

# docker-compose의 restart: unless-stopped 덕분에 서버 재부팅 시 자동 기동됨
```

---

## 15. 유용한 운영 명령어

```bash
# 로그 확인
docker compose -f docker-compose.prod.yml logs -f api

# API 컨테이너만 재시작
docker compose -f docker-compose.prod.yml restart api

# 전체 중지
docker compose -f docker-compose.prod.yml down

# 이미지 재빌드 후 배포
docker compose -f docker-compose.prod.yml up -d --build api

# 디스크 정리 (미사용 이미지 삭제)
docker system prune -f
```

---

## 16. CI/CD 자동 배포 (GitHub Actions)

`.github/workflows/deploy.yml` 예시:

```yaml
name: Deploy SkinAge API

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to server
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.SSH_HOST }}
          username: ${{ secrets.SSH_USER }}
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd /opt/skinage/SkinAge
            git pull origin main
            docker compose -f docker-compose.prod.yml up -d --build api
            docker compose -f docker-compose.prod.yml restart nginx
```

**GitHub Secrets 등록** (Settings → Secrets and variables → Actions):

| Secret 이름 | 값 |
|---|---|
| `SSH_HOST` | 서버 공인 IP |
| `SSH_USER` | SSH 접속 계정 (예: `ubuntu`) |
| `SSH_PRIVATE_KEY` | SSH 개인키 전체 내용 |

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| 서버 시작 후 60초간 503 | 모델 로딩 시간 | healthcheck의 `start_period`가 60s이므로 정상. 기다리세요 |
| `mediapipe` 관련 에러 | MediaPipe 모델 파일 미존재 | 첫 요청 시 자동 다운로드됨. 인터넷 연결 확인 |
| OOM Kill (메모리 부족) | 모델 + PyTorch 메모리 초과 | Step 8 스왑 설정. 또는 `SKINAGE_DEVICE=cpu` 확인 |
| certbot Timeout | 방화벽 미오픈 | 가비아 콘솔 + ufw 모두에서 80번 포트 확인 |
| `sed -i` 후 nginx 변경 안 됨 | inode 변경으로 bind mount 깨짐 | `docker compose restart nginx` |
| `Permission denied` 에러 | Docker 그룹 미적용 | `newgrp docker` 또는 재로그인 |
| GPU 미인식 | nvidia-docker 미설치 | `nvidia-smi` 확인 후 nvidia-container-toolkit 설치 |

---

## 아키텍처 요약

```
인터넷 → [가비아 방화벽] → [Nginx :80/:443]
                                    │
                                    ├── /api/*  →  [SkinAge API :8000]
                                    ├── /docs   →  [SkinAge API :8000]
                                    └── /health →  [SkinAge API :8000]
```

- **Nginx**: TLS 종단, 리버스 프록시, 파일 업로드 크기 제한
- **SkinAge API**: FastAPI + PyTorch 추론 서버
- **모델 파일**: `outputs/models/best_model.pth` (볼륨 마운트)
