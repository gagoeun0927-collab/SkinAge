# SkinAge 재배포 가이드

코드나 모델을 수정한 후 서버에 반영하는 절차입니다.

---

## 현재 배포 구조 요약

| 항목 | 값 |
|------|-----|
| 서버 | 가비아 Ubuntu (IP: `1.201.116.161`) |
| 도메인 | `skinage-api.duckdns.org` |
| 프로젝트 경로 (서버) | `/opt/skinage/SkinAge` |
| Docker Compose | `docker-compose.prod.yml` |
| API 컨테이너 | `skinage-api` (FastAPI, port 8000) |
| Nginx 컨테이너 | `skinage-nginx` (port 80/443) |
| GitHub 레포 | `gagoeun0927-collab/SkinAge` |
| CI/CD | GitHub Actions → SSH → 서버에서 build & deploy |

---

## 방법 1: 자동 배포 (코드 수정 시)

코드를 수정하고 `master`에 push하면 GitHub Actions가 자동으로 서버에 배포합니다.

```bash
# 로컬에서
git add .
git commit -m "fix: 수정 내용"
git push origin master
```

GitHub Actions가 알아서:
1. 서버에 SSH 접속
2. `git pull`
3. `docker compose up -d --build api`
4. nginx 재시작

**확인**: https://github.com/gagoeun0927-collab/SkinAge/actions 에서 실행 상태 확인

---

## 방법 2: 수동 배포 (서버에서 직접)

GitHub Actions 없이 서버에서 직접 배포할 때:

```bash
ssh ubuntu@1.201.116.161

cd /opt/skinage/SkinAge
git pull origin master
docker compose -f docker-compose.prod.yml up -d --build api
docker restart skinage-nginx
```

---

## 방법 3: 모델만 교체할 때

학습된 모델 파일만 바꾸는 경우 (Docker 재빌드 불필요):

```bash
# 1. 로컬에서 모델 파일 전송
scp -i <키파일> outputs/models/best_model.pth ubuntu@1.201.116.161:/opt/skinage/SkinAge/outputs/models/

# 2. 서버에서 API 컨테이너만 재시작 (모델을 다시 로드)
ssh ubuntu@1.201.116.161
docker restart skinage-api
```

> 모델은 볼륨 마운트(`./outputs/models:/app/outputs/models:ro`)로 연결되어 있어서
> 파일만 교체하고 컨테이너 재시작하면 반영됩니다. 이미지 재빌드 필요 없음.

---

## 방법 4: GitHub Actions 수동 트리거

push 없이 현재 코드를 다시 배포하고 싶을 때:

1. https://github.com/gagoeun0927-collab/SkinAge/actions
2. 좌측 **Deploy SkinAge API** 클릭
3. **Run workflow** → Branch `master` → **Run workflow**

---

## 상황별 정리

| 수정 내용 | 해야 할 것 |
|-----------|------------|
| Python 코드 수정 | `git push` → 자동 배포 (이미지 재빌드) |
| requirements.txt 수정 | `git push` → 자동 배포 (이미지 재빌드) |
| 모델 파일(best_model.pth) 교체 | scp로 전송 → `docker restart skinage-api` |
| config yaml 수정 | `git push` → 자동 배포 (이미지 재빌드) |
| nginx 설정 수정 | 서버에서 `active.conf` 수정 → `docker restart skinage-nginx` |
| Dockerfile 수정 | `git push` → 자동 배포 (이미지 재빌드) |
| 도메인 변경 | 서버에서 `active.conf` + certbot 재발급 |

---

## 배포 후 확인

```bash
# 서비스 상태
docker ps

# 헬스체크
curl https://skinage-api.duckdns.org/api/v1/health

# 로그 확인
docker logs skinage-api --tail 50
docker logs skinage-nginx --tail 20

# 분석 테스트
curl -X POST https://skinage-api.duckdns.org/api/v1/analyze \
  -F "file=@test_face.jpg"
```

---

## 롤백

문제가 생기면 이전 버전으로 되돌리기:

```bash
ssh ubuntu@1.201.116.161
cd /opt/skinage/SkinAge

# 이전 커밋으로 돌아가기
git log --oneline -5          # 돌아갈 커밋 SHA 확인
git checkout <커밋SHA> -- .   # 해당 버전으로 파일 복원

# 재빌드
docker compose -f docker-compose.prod.yml up -d --build api
docker restart skinage-nginx
```

---

## 주의사항

- `active.conf`는 git에 포함되지 않음 (서버에서 직접 관리). `git pull`로 덮어쓰이지 않음
- 모델 파일(`outputs/models/best_model.pth`)은 `.gitignore`에 포함 — git push로 배포 안 됨, scp 사용
- 서버 RAM이 부족하면 빌드 중 OOM 발생 가능 → 스왑 2GB 설정 확인 (`swapon --show`)
- MediaPipe 모델(`face_landmarker.task`, `blaze_face_short_range.tflite`)은 첫 요청 시 자동 다운로드
