"""PyInstaller 진입점 (패키지 상대 import를 쓰기 위해 바깥에 둔다)."""

from secret.app import main

raise SystemExit(main())
