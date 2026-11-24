"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

历史重放的凭据、转交、回执和审批接口是固定路径，必须排在通用
`/api/{module}` 动态路由之前注册，否则会被动态路径吞掉。
"""
from __future__ import annotations

from app.routers import exchange_replay, resource

ROUTERS = [exchange_replay, resource]
