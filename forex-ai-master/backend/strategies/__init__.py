"""Strategy registry."""

from backend.strategies.base import Strategy
from backend.strategies.breakout import Breakout
from backend.strategies.mean_reversion import MeanReversion
from backend.strategies.momentum import Momentum
from backend.strategies.pullback import Pullback
from backend.strategies.trend_following import TrendFollowing

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (TrendFollowing, Breakout, Pullback, MeanReversion, Momentum)
}

__all__ = ["STRATEGIES", "Strategy", "Breakout", "MeanReversion", "Momentum", "Pullback", "TrendFollowing"]
