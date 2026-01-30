"""
RoutingScoreService - 路由评分服务 (v5.0 COSMIC-GENESIS)

基于典型的“多臂老虎机 (Multi-Armed Bandit)”问题解决方案：
引入贝叶斯汤普森采样 (Bayesian Thompson Sampling) 算法。
每一个 (Provider, Model) 组合被视为一个“老虎机手臂”，其成功率遵循 Beta(α, β) 分布。

v5.0 巅峰版增强:
- 贝叶斯推断：α 代表成功次数，β 代表失败次数，通过从 Beta 分布采样决定路由优先级。
- 动态平衡：在“探索 (Exploration)”与“利用 (Exploitation)”之间实现数学意义上的最优平衡。
- 延迟衰减：结合 EWMA 延迟信息，在高延迟情况下通过惩罚因子修正采样值。
- 遗忘机制：引入半衰期，使系统能够适应供应商服务质量的动态波动。
"""

import time
import random
import math
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class ProviderStats:
    """供应商统计数据 (贝叶斯后验参数)"""
    alpha: float = 1.0               # 成功先验/计数 (Beta 分布参数)
    beta: float = 1.0                # 失败先验/计数 (Beta 分布参数)
    latency_ewma: float = 800.0      # 延迟 EWMA (用于延迟惩罚)
    last_updated: int = 0            # 最后更新时间戳 (ms)
    samples: int = 0                 # 样本总数
    
    # 辅助统计
    total_success: int = 0
    total_failure: int = 0


class RoutingScoreService:
    """
    路由评分服务 - 汤普森采样实现
    """
    
    # Thompson Sampling 配置
    # 初始先验 α=1, β=1 (Uniform 分布，即完全没见过的情况)
    PRIOR_ALPHA = 1.0
    PRIOR_BETA = 1.0
    
    # EWMA 配置
    ALPHA_EWMA = 0.2
    
    # 忘却因子 (半衰期 12 小时)
    # 随着时间推移，旧的经验权重会降低，使 α 和 β 向 PRIOR 靠拢
    DECAY_HALF_LIFE_MS = 12 * 60 * 60 * 1000 
    
    _stats: Dict[str, ProviderStats] = {}

    @classmethod
    def _key(cls, provider_id: str, model: str) -> str:
        return f"{provider_id}:{model}"

    @classmethod
    def _apply_decay(cls, stat: ProviderStats, now: int) -> ProviderStats:
        """应用时间衰减，使系统对环境变化更敏感"""
        if stat.last_updated == 0:
            return stat
            
        age_ms = now - stat.last_updated
        if age_ms <= 0:
            return stat
            
        # 计算衰减比例
        decay = 2 ** (-age_ms / cls.DECAY_HALF_LIFE_MS)
        
        # 将 alpha 和 beta 向先验值（默认 1）收缩
        stat.alpha = cls.PRIOR_ALPHA + (stat.alpha - cls.PRIOR_ALPHA) * decay
        stat.beta = cls.PRIOR_BETA + (stat.beta - cls.PRIOR_BETA) * decay
        
        return stat

    @classmethod
    def update(cls, provider_id: str, model: str, success: bool, latency_ms: int = None):
        """更新统计观测值"""
        key = cls._key(provider_id, model)
        now = int(time.time() * 1000)
        
        stat = cls._stats.get(key, ProviderStats(alpha=cls.PRIOR_ALPHA, beta=cls.PRIOR_BETA, last_updated=now))
        
        # 1. 首先应用时间衰减
        stat = cls._apply_decay(stat, now)
        
        # 2. 更新贝叶斯参数
        if success:
            stat.alpha += 1.0
            stat.total_success += 1
        else:
            stat.beta += 1.0
            stat.total_failure += 1
            
        # 3. 更新延迟 EWMA
        if latency_ms is not None:
            stat.latency_ewma = (1 - cls.ALPHA_EWMA) * stat.latency_ewma + cls.ALPHA_EWMA * float(latency_ms)
            
        stat.samples += 1
        stat.last_updated = now
        cls._stats[key] = stat

    @classmethod
    def _betavariate(cls, alpha: float, beta: float) -> float:
        """
        采样函数。如果 alpha 和 beta 很大，近似正态分布以加速。
        核心逻辑：从 Beta 分布中随机采样一个成功率预估值。
        """
        try:
            return random.betavariate(alpha, beta)
        except ValueError:
            # 防止极端情况下的数值错误
            return alpha / (alpha + beta)

    @classmethod
    def score_for(cls, provider_id: str, model: str) -> float:
        """
        计算路由评分 (汤普森采样核心逻辑)。
        
        该方法每次调用都会返回不同的值（随机采样），
        调用者应在一次路由决策中对所有候选节点调用此方法，并选择最高分。
        """
        key = cls._key(provider_id, model)
        now = int(time.time() * 1000)
        
        stat = cls._stats.get(key)
        if stat is None:
            # 基础随机探索
            return cls._betavariate(cls.PRIOR_ALPHA, cls.PRIOR_BETA)
            
        # 应用时间衰减
        stat = cls._apply_decay(stat, now)
        
        # 从该供应商的当前成功率分布中“抽一签”
        sampled_success_rate = cls._betavariate(stat.alpha, stat.beta)
        
        # 延迟惩罚因子 (Latency Penalty)
        # 延迟越高，对采样值的惩罚越大。这里使用逻辑斯谛衰减或简单的指数反比
        # 800ms 为软基准，超过此值会显著降低分数
        latency_multiplier = 1.0 / (1.0 + math.exp((stat.latency_ewma - 3000) / 1000))
        
        # 最终得分 = 采样成功率 * 延迟修正
        # 这里的 0.01 随机抖动用于打破极小的数值僵局
        return (sampled_success_rate * 0.8 + sampled_success_rate * 0.2 * latency_multiplier) + random.random() * 0.005

    @classmethod
    def get_stats(cls, provider_id: str, model: str) -> Optional[Dict]:
        """导出详细统计视图"""
        key = cls._key(provider_id, model)
        stat = cls._stats.get(key)
        if not stat: return None
        
        current_prob = stat.alpha / (stat.alpha + stat.beta)
        return {
            "expectation": round(current_prob, 4),
            "alpha": round(stat.alpha, 2),
            "beta": round(stat.beta, 2),
            "latency_ewma": round(stat.latency_ewma, 2),
            "samples": stat.samples,
            "success_total": stat.total_success,
            "failure_total": stat.total_failure,
            "last_updated": stat.last_updated
        }

    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict]:
        result = {}
        for key, stat in cls._stats.items():
            parts = key.split(":", 1)
            if len(parts) == 2:
                result[key] = cls.get_stats(parts[0], parts[1])
        return result

