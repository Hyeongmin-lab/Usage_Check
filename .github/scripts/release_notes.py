"""릴리스 노트 = 다운로드 안내 + CHANGELOG 의 해당 버전 부분 + 체크섬."""
import re
import sys
from pathlib import Path

tag, out = sys.argv[1], Path(sys.argv[2])
ver = tag.lstrip("v")
log = Path("CHANGELOG.md").read_text(encoding="utf-8")
m = re.search(rf"^## v{re.escape(ver)}\b.*?(?=^## |\Z)", log, re.S | re.M)
changes = m.group(0).strip() if m else "변경 사항은 CHANGELOG.md 를 참고하세요."
sums = Path("out/SHA256SUMS.txt").read_text(encoding="ascii").strip()
repo = "Hyeongmin-lab/Usage_Check"
body = f"""## 다운로드

| 파일 | 누구에게 |
|---|---|
| **AIQuota.exe** | 대부분 이걸로. 더블클릭하면 바로 위젯이 떠요 (Windows 10/11, Python 설치 불필요) |
| AIQuota-python.zip | Python이 있고 코드를 직접 돌리고 싶은 분 (`위젯_실행.bat`) |

**처음 실행할 때** "Windows의 PC 보호" 창이 뜰 수 있어요. 코드 서명 인증서가 없는 개인 프로젝트라서 그래요 → **추가 정보 → 실행**을 누르세요.
그 전에 아래 방법으로 파일이 이 저장소에서 빌드된 게 맞는지 확인할 수 있어요.

## 이 파일이 진짜 이 저장소 코드로 만들어졌는지 확인

이 파일들은 개인 PC가 아니라 **GitHub Actions가 공개된 코드로 직접 빌드**했고, GitHub가 그 출처를 서명해 두었어요.

- 출처 확인 (GitHub CLI): `gh attestation verify AIQuota.exe -R {repo}`
- 체크섬 확인 (PowerShell): `Get-FileHash .\\AIQuota.exe -Algorithm SHA256` → 아래 값과 같아야 해요

```
{sums}
```

{changes}

---
보안 설계와 검수 결과: [SECURITY.md](https://github.com/{repo}/blob/{tag}/SECURITY.md)
"""
out.write_text(body, encoding="utf-8")
print(body)
