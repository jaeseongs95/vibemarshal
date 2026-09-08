"""FlowMarshal Engine 배포 패키지.

배포 wheel은 :mod:`flowmarshal.engine`과 그 canonical helper만 제공한다.
legacy/prototype·평가 도구는 source-tree 개발 계약으로 분리하며, 최상위
import가 그 경계를 자동으로 섞지 않는다.
"""

__all__: list[str] = []

__version__ = "0.2.0a1"
