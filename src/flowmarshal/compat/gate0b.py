"""Gate 0B 호환 API.

이 모듈은 역사적 Gate 0B artifact와 테스트를 재현할 때만 사용한다.
새 제품 코드는 ``flowmarshal.core``를 사용하며 HMAC authority 또는 파일별
AccessGrant를 실행 전제조건으로 두지 않는다.
"""

from ..adapters.authority import FileHumanControlAuthority
from ..adapters.sqlite import SQLiteLedger
from ..application import FlowMarshalService
from ..domain import AccessMode, ResourceDefinition, ResourceKind

LegacyGate0BService = FlowMarshalService
LegacyGate0BLedger = SQLiteLedger

__all__ = [
    "AccessMode",
    "FileHumanControlAuthority",
    "LegacyGate0BLedger",
    "LegacyGate0BService",
    "ResourceDefinition",
    "ResourceKind",
]
