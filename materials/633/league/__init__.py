"""虚构联赛的赛程生产服务与赛季排名仓。

- :class:`~league.schedule_service.ScheduleService`：赛程数据生产服务。
- :class:`~league.standings_repo.StandingsRepository`：赛季排名仓（比分/判罚事实）。
- :mod:`league.ranking`：不依赖存储的纯函数排名引擎，可独立用于复算与断链对照。
"""

from .schedule_service import ScheduleService, Issue, CandidateRejected
from .standings_repo import StandingsRepository, BatchRejected
from .ranking import compute_standings

__all__ = [
    "ScheduleService",
    "Issue",
    "CandidateRejected",
    "StandingsRepository",
    "BatchRejected",
    "compute_standings",
]
