# 인스타그램 릴스 분석 — 세팅 안내

릴스 지표를 Instagram Graph API로 직접 받아와 대시보드와 분석 보고서를 만든다.
파이썬 표준 라이브러리만 쓰므로 `pip install` 할 것이 없다.

---

## 0. 준비물 확인

- **비즈니스 또는 크리에이터 계정**이어야 한다. 개인 계정은 조회수·저장 지표가 나오지 않는다.
  (인스타 앱 → 설정 → 계정 유형 및 도구 → 프로페셔널 계정으로 전환)
- 파이썬 3.9 이상

---

## 1. Meta 앱 만들기

1. <https://developers.facebook.com/apps> 접속 → **앱 만들기**
2. 사용 사례에서 **기타(Other)** → 앱 유형 **비즈니스(Business)**
3. 앱 이름은 아무거나 (예: `reels-dashboard`). 이 앱은 나 혼자 쓸 것이므로 심사·공개가 필요 없다.
4. 앱 대시보드에서 **제품 추가** → **Instagram** → **설정**

## 2. 인스타 계정 연결

"Instagram" 제품 화면의 **API 설정** 탭에서 두 가지 길이 나온다.

| 방식 | 조건 | 비고 |
| --- | --- | --- |
| **Instagram 로그인으로 설정** | 페이스북 페이지 불필요 | **이쪽을 권한다** |
| 페이스북 로그인으로 설정 | 인스타 계정이 FB 페이지에 연결돼 있어야 함 | 기존에 연결돼 있다면 이것도 무방 |

**Instagram 로그인** 쪽을 눌러:

1. **비즈니스 로그인 설정** → 리디렉션 URI에 `https://localhost/` 를 넣고 저장
2. 권한(스코프)에 아래 세 개가 들어 있는지 확인
   - `instagram_business_basic`
   - `instagram_business_manage_insights`
   - (댓글까지 읽고 싶다면 `instagram_business_manage_comments`)
3. **Instagram 테스터 추가**가 필요하면 내 계정을 테스터로 추가하고, 인스타 앱
   → 설정 → 웹사이트 권한 → 테스터 초대 수락

## 3. 액세스 토큰 받기

같은 화면 아래 **액세스 토큰 생성**(Generate token) 버튼이 있다. 내 계정을 골라 로그인하면
토큰 문자열이 나온다. **이것을 복사한다.**

- 여기서 나오는 토큰은 보통 **장기 토큰(60일)** 이다.
- 60일이 지나기 전에 갱신해야 한다. 아래 5번의 `--refresh-token`이 그 일을 한다.

> 버튼이 보이지 않는다면 <https://developers.facebook.com/tools/explorer> (그래프 API 탐색기)에서
> 내 앱을 고르고 위 스코프를 체크해 단기 토큰을 받은 뒤,
> `python3 collect.py --exchange-token <단기토큰>` 으로 장기 토큰으로 바꾼다.

## 4. 토큰 저장

이 폴더에 `.env` 파일을 만든다. (git에 올라가지 않도록 이미 제외해 두었다)

```sh
cp .env.example .env
```

`.env` 를 열어 토큰을 붙여넣는다.

```
IG_ACCESS_TOKEN=여기에_토큰_붙여넣기
```

`IG_USER_ID`는 비워 두어도 된다. 스크립트가 알아서 찾는다.
페이스북 로그인 방식으로 연결했다면 `IG_LOGIN=facebook` 한 줄을 더 넣는다.

## 5. 실행

```sh
cd instagram

# 연결 확인 — 계정 이름과 팔로워 수가 찍히면 성공
python3 collect.py --check

# 최근 릴스 30개 수집 (썸네일 포함)
python3 collect.py --limit 30

# 대시보드 만들기
python3 build_dashboard.py

# 토큰 갱신 (50일쯤마다 한 번)
python3 collect.py --refresh-token
```

`dashboard.html` 이 생긴다. 브라우저로 열면 된다.

## 6. 매일 자동 수집

계정 추이(팔로워 변화, 일별 조회수)는 매일 한 번씩 찍어 두어야 선이 그려진다.
crontab에 아래 한 줄을 넣는다. (`crontab -e`)

```
0 9 * * * cd /경로/instagram && /usr/bin/python3 collect.py --limit 30 --quiet && /usr/bin/python3 build_dashboard.py --quiet
```

매일 오전 9시에 돈다. 대시보드의 「자동 수집」 패널이 마지막 수집 시각을 보여주므로,
cron이 살아 있는지는 거기서 확인하면 된다.

---

## 쌓이는 파일

| 파일 | 내용 |
| --- | --- |
| `data/reels.json` | 릴스별 지표. 실행할 때마다 최신값으로 덮어쓰되, 과거 스냅샷은 `history`에 남는다 |
| `data/daily.json` | 날짜별 팔로워 수·계정 조회수·도달. 하루 한 줄씩 쌓인다 |
| `data/thumbs/` | 릴스 썸네일 |
| `data/account.json` | 계정 프로필 정보 |
| `report/report.md` | 종합 분석 보고서 원고 (사람이 쓰거나 Claude가 쓴다) |

`data/` 는 기본적으로 git에서 제외돼 있다. 여러 기기에서 이력을 공유하고 싶다면
`.gitignore` 에서 해당 줄을 지우면 된다. (내 계정 지표가 저장소에 올라간다는 점은 유의)

---

## 안 될 때

**`(#100) ... metrics should not be specified`**
→ 해당 지표가 그 게시물 유형에 없다는 뜻이다. 스크립트가 자동으로 그 지표를 빼고 다시 부르므로
   그대로 두어도 된다. 결과에는 `N/A`로 남는다.

**`Object with ID ... does not exist`**
→ 토큰이 다른 계정 것이거나 만료됐다. `--check`로 확인하고 3번부터 다시.

**`(#10) Application does not have permission for this action`**
→ 스코프에 `instagram_business_manage_insights`가 빠졌다. 2번에서 다시 확인.

**릴스가 하나도 안 잡힌다**
→ `media_product_type`이 `REELS`인 것만 모은다. 예전에 올린 일반 동영상은 `VIDEO`라 빠진다.
   `--include-video` 를 붙이면 같이 모은다.

**`views`가 전부 N/A**
→ 계정이 개인 계정이거나, 프로페셔널로 바꾼 지 얼마 안 됐을 수 있다. 전환 이전 게시물은
   인사이트가 없다.
