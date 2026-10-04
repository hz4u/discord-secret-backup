from ..core.config_store import ConfigCorruptError
from ..core.crypto_core import DecryptionError, FormatError, UnsupportedVersionError
from ..core.discord_api import DiscordAuthError, DiscordError, DiscordNotFound, DiscordPermissionError, NetworkError
from ..core.file_crypto import FileTooLargeError
from ..core.manifest import ManifestError
from ..core.restore import NoBackupError
from ..i18n import tr


def counts_text(pairs, empty: str = "") -> str:
    return " · ".join(tr("{name} {n}개", name=name, n=n) for name, n in pairs if n) or empty


def friendly(exc: BaseException) -> str:
    if isinstance(exc, ConfigCorruptError):
        return tr("설정 파일이 손상되었습니다.")
    if isinstance(exc, DecryptionError):
        return tr("비밀번호가 틀렸거나 데이터가 손상되었습니다.")
    if isinstance(exc, UnsupportedVersionError):
        return tr("지원하지 않는 버전의 암호문입니다.")
    if isinstance(exc, FormatError):
        return tr("ENC1 암호문 형식이 아닙니다. 디스코드에서 전체를 복사했는지 확인하세요.")
    if isinstance(exc, FileTooLargeError):
        return tr("파일이 너무 큽니다. 도구의 파일 암호화는 최대 200MB까지입니다.")
    if isinstance(exc, ManifestError):
        return tr("비밀번호가 틀렸거나 디스코드의 목차가 손상되었습니다.")
    if isinstance(exc, NoBackupError):
        return tr("이 서버에서 Secret 백업을 찾지 못했습니다.")
    if isinstance(exc, DiscordAuthError):
        return tr("봇 토큰이 올바르지 않습니다.")
    if isinstance(exc, DiscordNotFound):
        return tr("서버를 찾을 수 없습니다. 서버 ID가 맞는지, 봇이 그 서버에 초대되었는지 확인하세요.")
    if isinstance(exc, DiscordPermissionError):
        return tr("봇 권한이 부족합니다. 연결 설정에서 권한 체크리스트를 확인하세요.")
    if isinstance(exc, NetworkError):
        return tr("디스코드에 연결할 수 없습니다. 인터넷 연결을 확인하세요.")
    if isinstance(exc, DiscordError):
        return tr("디스코드 요청이 실패했습니다. ({exc})", exc=exc)
    if isinstance(exc, OSError):
        return tr("파일을 읽거나 쓸 수 없습니다. ({v1})", v1=exc.strerror or exc)
    return tr("알 수 없는 오류가 발생했습니다. ({exc!r})", exc=exc)
